import os
import re
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as crequests
    HAS_CURL = True
except ImportError:
    import requests as crequests
    HAS_CURL = False

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7'
}

MAX_RESULTS = 50

BAD_IMG_KEYWORDS = [
    'logo', 'icon', 'hey-obi', 'bieber', 'biber', 'footer', 'header',
    'banner', 'app', 'placeholder', 'svg', 'rating', 'star', 'wishlist',
    'heart', 'herz', 'avatar', 'badge', 'stiftung', 'warentest',
    'newsletter', 'trust', 'siegel', 'pay', 'klarna', 'paypal', 'visa'
]

# ===================================================================
# SESSIONS
# ===================================================================
def get_amazon_session():
    session = crequests.Session(impersonate="chrome120") if HAS_CURL else crequests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7'
    })
    session.cookies.set("i18n-prefs", "EUR", domain=".amazon.de")
    session.cookies.set("lc-main", "de_DE", domain=".amazon.de")
    return session

def get_session():
    session = crequests.Session(impersonate="chrome120") if HAS_CURL else crequests.Session()
    session.headers.update(HEADERS)
    return session

# ===================================================================
# HELPER FUNCTIONS
# ===================================================================
def format_price_string(text):
    if not text: return "-"
    text = str(text).replace('$', '').replace('USD', '').replace('EUR', '').replace('€', '').strip()
    match = re.search(r'(\d{1,4}[.,]\d{2})', text)
    if match: return f"{match.group(1).replace('.', ',')} €"
    match_int = re.search(r'^(\d{1,4})$', text)
    if match_int: return f"{match_int.group(1)},00 €"
    return "-"

def is_valid_image(url):
    if not url or not isinstance(url, str): return False
    url_l = url.lower()
    if not url_l.startswith('http') or url_l.startswith('data:image'): return False
    return not any(bad in url_l for bad in BAD_IMG_KEYWORDS)

def deduplicate(products):
    seen, unique = set(), []
    for p in products:
        link_clean = p['link'].lower()
        if link_clean not in seen:
            seen.add(link_clean)
            unique.append(p)
    return unique

def extract_product_tiles(html, domain, url_validator):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['header', 'footer', 'nav', 'aside', 'menu']):
        tag.decompose()
       
    products, seen = [], set()

    for a in soup.find_all('a', href=True):
        link = a['href']
        if link.startswith('//'): link = 'https:' + link
        elif link.startswith('/'): link = f"https://www.{domain}" + link
        link = link.split('?')[0].split('#')[0]
       
        if not url_validator(link) or link in seen: continue
       
        container = a
        for _ in range(3):
            if container.parent and container.parent.name not in ['body', 'html']:
                if len(container.parent.get_text(strip=True)) < 600:
                    container = container.parent
                else: break
            else: break
               
        title = ""
        for img_cand in container.find_all('img'):
            alt = img_cand.get('alt', '').strip()
            if alt and len(alt) > 6 and not any(bad in alt.lower() for bad in BAD_IMG_KEYWORDS):
                title = alt
                break
               
        if not title:
            hx = container.find(['h1', 'h2', 'h3', 'h4', 'h5'])
            if hx and len(hx.get_text(strip=True)) > 5: title = hx.get_text(separator=' ', strip=True)
               
        if not title: title = a.get_text(separator=' ', strip=True)
        title = re.sub(r'\s+', ' ', title).strip()
        title = re.sub(r'(In den Warenkorb|Auf die Wunschliste|Merken|Hinzufügen).*$', '', title, flags=re.I).strip()
       
        if len(title) < 4: continue
           
        img_url = ""
        for img_tag in container.find_all(['img', 'source']):
            src = (img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-srcset') or img_tag.get('srcset') or '')
            if src:
                src = src.split(',')[0].split(' ')[0].strip()
                if src.startswith('//'): src = 'https:' + src
                elif src.startswith('/'): src = f"https://www.{domain}" + src
                if is_valid_image(src):
                    img_url = src
                    break
                   
        price = "-"
        m = re.search(r'(\d{1,4}[.,]\d{2})\s*€?', container.get_text(separator=' '))
        if m: price = format_price_string(m.group(1))
           
        products.append({"title": title, "price": price, "imageUrl": img_url, "link": link})
        seen.add(link)
       
    return products

# Fallback-Suchmaschine mit reinen Domain-Suchen
def execute_external_search(session, domain, keyword, valid_url_func):
    products, seen = [], set()
    q = f"site:{domain} {keyword}"

    # 1. DuckDuckGo HTML
    try:
        res = session.post("https://html.duckduckgo.com/html/", data={"q": q}, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'html.parser')
            for a in soup.find_all('a', class_='result__url'):
                href = a.get('href', '')
                if 'uddg=' in href:
                    href = urllib.parse.unquote(href.split('uddg=')[1].split('&')[0])
                href = href.split('?')[0].split('#')[0]
                if valid_url_func(href) and href not in seen:
                    container = a.find_parent('div', class_='result')
                    title_elem = container.find('h2', class_='result__title') if container else None
                    title = title_elem.get_text(strip=True) if title_elem else a.get_text(strip=True)
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                    if len(title) > 4:
                        products.append({"title": title, "price": "-", "imageUrl": "", "link": href})
                        seen.add(href)
    except Exception: pass

    if len(products) >= MAX_RESULTS: return products[:MAX_RESULTS]

    # 2. Yahoo Search
    try:
        res = session.get(f"https://search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'html.parser')
            for a in soup.find_all('a', href=True):
                link = a['href']
                if 'RU=' in link:
                    try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                    except: pass
                link = link.split('?')[0].split('#')[0]
                if valid_url_func(link) and link not in seen:
                    title = a.get_text(strip=True)
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                    if len(title) > 4:
                        products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
                        seen.add(link)
    except Exception: pass

    return products[:MAX_RESULTS]

# Generic Enricher für unvollständige Kacheln
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p
    try:
        res = session.get(p['link'], timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # JSON-LD Parser
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string or '')
                    items = data.get('@graph', [data]) if isinstance(data, dict) else (data if isinstance(data, list) else [data])
                    for sub in items:
                        if isinstance(sub, dict) and any(t in sub.get('@type', '') for t in ['Product', 'IndividualProduct', 'ItemPage']):
                            if not p.get('imageUrl') or not is_valid_image(p.get('imageUrl')):
                                img = sub.get('image')
                                if isinstance(img, list) and img: img = img[0]
                                if isinstance(img, dict): img = img.get('url') or img.get('contentUrl')
                                if isinstance(img, str) and img.startswith('http') and is_valid_image(img):
                                    p['imageUrl'] = img

                            if p.get('price') == '-':
                                offers = sub.get('offers')
                                if isinstance(offers, list) and offers: offers = offers[0]
                                if isinstance(offers, dict):
                                    pr = offers.get('price') or offers.get('lowPrice')
                                    if pr: p['price'] = format_price_string(str(pr))
                except Exception: pass

            # Shop-spezifische Fallbacks
            if shop_key == 'otto':
                if not p.get('imageUrl') or not is_valid_image(p.get('imageUrl')):
                    m_otto_img = re.search(r'["\'](https://i\.otto\.de/i/otto/[^"\']+)["\']', html)
                    if m_otto_img: p['imageUrl'] = m_otto_img.group(1)
                if p.get('price') == '-':
                    m_otto_pr = re.search(r'data-qa=["\']price["\'][^>]*>([^<]+)', html) or re.search(r'["\']price["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', html) or re.search(r'(\d+[\.,]\d{2})\s*€', html)
                    if m_otto_pr: p['price'] = format_price_string(m_otto_pr.group(1))

            if shop_key == 'smythtoys':
                if not p.get('imageUrl') or not is_valid_image(p.get('imageUrl')):
                    m_smyth_img = re.search(r'["\'](https://image\.smythstoys\.com/[^"\']+)["\']', html)
                    if m_smyth_img: p['imageUrl'] = m_smyth_img.group(1)
                if p.get('price') == '-':
                    m_smyth_pr = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([\d\.]+)["\']', html) or re.search(r'class=["\']price_main["\'][^>]*>([^<]+)', html) or re.search(r'(\d+[\.,]\d{2})\s*€', html)
                    if m_smyth_pr: p['price'] = format_price_string(m_smyth_pr.group(1))

            if shop_key == 'obi' and (not p.get('imageUrl') or not is_valid_image(p.get('imageUrl'))):
                m_obi = re.search(r'["\'](https://(?:media|assets)\.obi\.de/[^"\']*?(?:jpg|png|webp|jpeg))["\']', html, re.I)
                if m_obi and is_valid_image(m_obi.group(1)): p['imageUrl'] = m_obi.group(1).replace('\\u002F', '/')

            if not p.get('imageUrl') or not is_valid_image(p.get('imageUrl')):
                img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if img and img.get('content') and is_valid_image(img['content']):
                    p['imageUrl'] = img['content']

            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'): p['price'] = format_price_string(og_p['content'])

    except Exception: pass
    return p

def enrich_products_parallel(session, products, shop_key):
    items = [p for p in products if not p.get('imageUrl') or p.get('price') == '-'][:MAX_RESULTS]
    if items:
        workers = 2 if shop_key == 'otto' else 5
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(enrich_single_product, p, session, shop_key) for p in items]
            for f in futures:
                try: f.result()
                except Exception: pass

    cleaned = []
    for p in products:
        if not is_valid_image(p.get('imageUrl')): p['imageUrl'] = ""
        if shop_key in ['jysk', 'smythtoys'] and p['price'] == '-' and not p['imageUrl']: continue
        cleaned.append(p)

    return cleaned


# ===================================================================
# ISOLIERTE ROUTINEN FÜR ALLE ERFOLGS-SHOPS (UNANTASTBAR)
# ===================================================================

# 1. AMAZON (ISOLIERT)
def scrape_amazon(keyword):
    session = get_amazon_session()
    products = []
    for page in [1, 2, 3]:
        if len(products) >= MAX_RESULTS: break
        try:
            url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&s=exact-aware-popularity-rank&page={page}"
            res = session.get(url, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content, 'html.parser')
                for item in soup.find_all('div', {'data-component-type': 's-search-result'}):
                    if len(products) >= MAX_RESULTS: break
                    asin = item.get('data-asin', '').strip()
                    if not asin or len(asin) != 10: continue
                    title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                    if not title_tag: continue
                   
                    price = "-"
                    p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('span.a-price')
                    if p_elem: price = format_price_string(p_elem.get_text())

                    img_tag = item.select_one('img.s-image')
                    img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else f"https://images-eu.ssl-images-amazon.com/images/P/{asin}.03._SCLZZZZZZZ_SX500_.jpg"
                   
                    products.append({"title": title_tag.get_text().strip(), "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"})
        except Exception: pass
    return deduplicate(products)[:MAX_RESULTS]

# 2. NORMA (ISOLIERT)
def scrape_norma(keyword):
    session = get_session()
    domain = 'norma24.de'
    def v(u): return domain in u.lower() and not any(b in u.lower() for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto'])
    products = []
    try:
        res = session.get(f"https://www.norma24.de/suche?q={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'norma')

# 3. NETTO (ISOLIERT MIT RESTORED-LOGIK)
def scrape_netto(keyword):
    session = get_session()
    domain = 'netto-online.de'
    def v(u):
        l = u.lower()
        if domain not in l: return False
        if any(b in l for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto', '/service']): return False
        return any(p in l for p in ['/p/', '/artikel/', '/p-']) or len(l.split('/')) > 4
    products = []
    try:
        res = session.get(f"https://www.netto-online.de/s/?query={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'netto')

# 4. OBI (ISOLIERT)
def scrape_obi(keyword):
    session = get_session()
    domain = 'obi.de'
    def v(u): return domain in u.lower() and any(p in u.lower() for p in ['/p/', '/product/', '/artikel/'])
    products = []
    try:
        res = session.get(f"https://www.obi.de/search/{urllib.parse.quote(keyword)}/?sort=relevance", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'obi')

# 5. KAUFLAND (ISOLIERT)
def scrape_kaufland(keyword):
    session = get_session()
    domain = 'kaufland.de'
    def v(u): return domain in u.lower() and not any(b in u.lower() for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto'])
    products = []
    try:
        res = session.get(f"https://www.kaufland.de/s/?search_value={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'kaufland')

# 6. OTTO (ISOLIERT)
def scrape_otto(keyword):
    session = get_session()
    domain = 'otto.de'
    def v(u): return domain in u.lower() and ('/p/' in u.lower() or '#variationid=' in u.lower())
    products = []
    try:
        res = session.get(f"https://www.otto.de/suche/{urllib.parse.quote(keyword)}/?sort=bestseller", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'otto')

# 7. BAUHAUS (ISOLIERT)
def scrape_bauhaus(keyword):
    session = get_session()
    domain = 'bauhaus.info'
    def v(u): return domain in u.lower() and not any(b in u.lower() for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto'])
    products = []
    try:
        res = session.get(f"https://www.bauhaus.info/suche/produkte?q={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200: products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, v)
    except Exception: pass
    if len(products) < 10: products.extend(execute_external_search(session, domain, keyword, v))
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'bauhaus')


# ===================================================================
# GENERIC ROUTINE (Nur noch für die restlichen SPA-Problemfälle)
# ===================================================================
def scrape_generic(session, shop_key, domain, keyword):
    search_urls = {
        'hm': f"https://www2.hm.com/de_de/search-results.html?q={urllib.parse.quote(keyword)}",
        'ikea': f"https://www.ikea.com/de/de/search/products/?q={urllib.parse.quote(keyword)}",
        'decathlon': f"https://www.decathlon.de/search?Ntt={urllib.parse.quote(keyword)}",
        'cna': f"https://www.c-and-a.com/de/de/shop/search?q={urllib.parse.quote(keyword)}",
        'smythtoys': f"https://www.smythstoys.com/de/de-de/search/?text={urllib.parse.quote(keyword)}",
        'jysk': f"https://jysk.de/search?query={urllib.parse.quote(keyword)}"
    }

    def valid_generic(u):
        l = u.lower()
        if domain not in l: return False
        if l.rstrip('/').endswith(domain): return False
       
        if any(b in l for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto', '/service', '/help', '/blog/', '/search', '/suche', '/kategorie']):
            return False
           
        # Spezifische Pfadfilter
        if shop_key == 'smythtoys':
            if '/c/' in l: return False # Blockiert Kategorieseiten
            if not any(p in l for p in ['/p/', '/product/']) and not re.search(r'\d{5,}', l): return False

        if shop_key == 'jysk' and len([p for p in urllib.parse.urlparse(l).path.split('/') if p]) < 2: return False
        if shop_key == 'ikea' and not any(p in l for p in ['/p/', '/pe', '/art/', '/products/']): return False
        if shop_key == 'hm' and 'productpage' not in l and '/product/' not in l: return False
        if shop_key == 'decathlon' and '/p/' not in l and '/mp/' not in l: return False
        if shop_key == 'cna' and not any(p in l for p in ['/product/', '/shop/', '/de/de/']): return False
       
        return True

    products = []
    url = search_urls.get(shop_key, f"https://www.{domain}/suche?q={urllib.parse.quote(keyword)}")
   
    try:
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, valid_generic)
    except Exception: pass

    if len(products) < 10:
        products.extend(execute_external_search(session, domain, keyword, valid_generic))

    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], shop_key)


# ===================================================================
# ROUTING
# ===================================================================
SHOP_DOMAINS = {
    'amazon': 'amazon.de', 'norma': 'norma24.de', 'netto': 'netto-online.de',
    'obi': 'obi.de', 'hm': 'hm.com', 'ikea': 'ikea.com', 'jysk': 'jysk.de',
    'kaufland': 'kaufland.de', 'otto': 'otto.de', 'smythtoys': 'smythstoys.com',
    'decathlon': 'decathlon.de', 'cna': 'c-and-a.com', 'bauhaus': 'bauhaus.info'
}

@app.route('/')
def home():
    return jsonify({"status": "online", "message": "Multi-Shop Scraper API Live"})

@app.route('/ping')
def ping():
    return jsonify({"status": "ready"})

@app.route('/scrape', methods=['GET'])
def scrape():
    shop_key = request.args.get('shop', '').lower().strip()
    keyword = request.args.get('keyword', '').strip()

    if not keyword or not shop_key or shop_key not in SHOP_DOMAINS:
        return jsonify({"error": "Ungültige Parameter"}), 400

    # Aufruf der vollkommen isolierten Routinen
    if shop_key == 'amazon': products = scrape_amazon(keyword)
    elif shop_key == 'norma': products = scrape_norma(keyword)
    elif shop_key == 'netto': products = scrape_netto(keyword)
    elif shop_key == 'obi': products = scrape_obi(keyword)
    elif shop_key == 'kaufland': products = scrape_kaufland(keyword)
    elif shop_key == 'otto': products = scrape_otto(keyword)
    elif shop_key == 'bauhaus': products = scrape_bauhaus(keyword)
    else:
        session = get_session()
        products = scrape_generic(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success", "shop": shop_key, "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword, "count": len(products), "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
