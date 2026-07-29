import os
import re
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
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'Sec-Ch-Ua': '"Google Chrome";v="125", "Chromium";v="125", "Not.A/Brand";v="24"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Upgrade-Insecure-Requests': '1'
}

def get_session():
    if HAS_CURL:
        session = crequests.Session(impersonate="chrome120")
    else:
        session = crequests.Session()
    session.headers.update(HEADERS)
    session.cookies.set("i18n-prefs", "EUR", domain=".amazon.de")
    session.cookies.set("lc-main", "de_DE", domain=".amazon.de")
    return session

def format_price_string(text):
    if not text: return "-"
    text = text.replace('$', '').replace('USD', '').replace('EUR', '').replace('€', '').strip()
    match = re.search(r'(\d{1,4}[.,]\d{2})', text)
    if match: return f"{match.group(1).replace('.', ',')} €"
    match_int = re.search(r'^(\d{1,4})$', text)
    if match_int: return f"{match_int.group(1)},00 €"
    return "-"

def get_amazon_image_url(asin):
    return f"https://images-eu.ssl-images-amazon.com/images/P/{asin}.03._SCLZZZZZZZ_SX500_.jpg"

def deduplicate(products):
    seen, unique = set(), []
    for p in products:
        link_clean = p['link'].lower()
        if link_clean not in seen:
            seen.add(link_clean)
            unique.append(p)
    return unique


# ===================================================================
# GEHÄRTETER PRODUKT-EXTRAKTOR (Filtert Wunschlisten & Herz-Icons)
# ===================================================================
def extract_product_tiles(html, domain, url_validator):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['header', 'footer', 'nav', 'aside', 'form', 'menu']):
        tag.decompose()
       
    products = []
    seen = set()
   
    BAD_WORDS = ['newsletter', 'datenschutz', 'impressum', 'agb', 'wunschzettel', 'wishlist', 'anmelden', 'mein konto', 'warenkorb', 'hinzufügen', 'merken']

    for a in soup.find_all('a', href=True):
        link = a['href']
        aria_label = (a.get('aria-label') or '').lower()
        a_text = a.get_text(strip=True).lower()
       
        # Wunschlisten-Buttons überspringen
        if any(w in aria_label for w in ['wunschliste', 'wishlist', 'merken']) or any(w in a_text for w in ['wunschliste', 'wishlist']):
            continue

        if link.startswith('//'): link = 'https:' + link
        elif link.startswith('/'): link = f"https://www.{domain}" + link
        link = link.split('?')[0].split('#')[0]
       
        if not url_validator(link) or link in seen: continue
       
        container = a
        for _ in range(4):
            if container.parent and container.parent.name not in ['body', 'html']:
                if len(container.parent.get_text(strip=True)) < 400:
                    container = container.parent
                else: break
            else: break
               
        # 1. TITEL
        title = ""
        for img_cand in container.find_all('img'):
            alt = img_cand.get('alt', '')
            if alt and len(alt) > 8 and not any(w in alt.lower() for w in BAD_WORDS):
                title = alt
                break
               
        if not title:
            hx = container.find(['h2', 'h3', 'h4', 'h5'])
            if hx and len(hx.get_text(strip=True)) > 8:
                title = hx.get_text(separator=' ', strip=True)
               
        if not title:
            title = a.get_text(separator=' ', strip=True)
           
        title = re.sub(r'\s+', ' ', title).strip()
        if len(title) < 5 or any(w in title.lower() for w in BAD_WORDS): continue
           
        # 2. BILD (Herz-Icons und SVGs filtern)
        img_url = ""
        for img_tag in container.find_all('img'):
            src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('srcset')
            if src:
                src = src.split(',')[0].split(' ')[0]
                if any(bad_img in src.lower() for bad_img in ['heart', 'herz', 'icon', 'wishlist', 'placeholder', '.svg']):
                    continue
                if src.startswith('//'): src = 'https:' + src
                elif src.startswith('/'): src = f"https://www.{domain}" + src
                if not src.startswith('data:image'):
                    img_url = src
                    break
                   
        # 3. PREIS
        price = "-"
        m = re.search(r'(\d{1,4})[.,](\d{2})\s*€?', container.get_text(separator=' '))
        if m: price = f"{m.group(1)},{m.group(2)} €"
           
        products.append({"title": title, "price": price, "imageUrl": img_url, "link": link})
        seen.add(link)
       
    return products


# ===================================================================
# DRITTANBIETER-SUCHE (DDG HTML + Bing + Yahoo Kaskade)
# ===================================================================
def execute_external_search(session, domain, keyword, valid_url_func):
    products = []
    q = f"site:{domain} {keyword}"

    # 1. DuckDuckGo HTML
    try:
        res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.find_all('a', class_='result__url', href=True):
                link = a['href']
                if 'uddg=' in link:
                    try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    except: pass
                link = link.split('?')[0]
                if valid_url_func(link):
                    parent = a.find_parent('div', class_='result')
                    t = parent.find('a', class_='result__a').get_text(strip=True) if parent and parent.find('a', class_='result__a') else a.get_text(strip=True)
                    products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', t), "price": "-", "imageUrl": "", "link": link})
    except: pass
    if len(products) >= 15: return products

    # 2. Bing
    try:
        res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for li in soup.find_all('li', class_='b_algo'):
                a = li.find('a', href=True)
                if a and valid_url_func(a['href']):
                    products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)), "price": "-", "imageUrl": "", "link": a['href'].split('?')[0]})
    except: pass
    if len(products) >= 15: return products

    # 3. Yahoo
    try:
        res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=6)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for div in soup.find_all('div', class_='algo'):
                a = div.find('a', href=True)
                if not a: continue
                link = a['href']
                if 'RU=' in link:
                    try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                    except: pass
                if valid_url_func(link):
                    products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)), "price": "-", "imageUrl": "", "link": link.split('?')[0]})
    except: pass

    return products


# ===================================================================
# ENRICHER (Verarbeitet jetzt ALLE 30 Produkte ohne Limit)
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p
    try:
        res = session.get(p['link'], timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            if not p.get('imageUrl'):
                img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if img and img.get('content') and not any(bad in img['content'].lower() for bad in ['heart', 'herz', 'icon', 'logo']):
                    p['imageUrl'] = img['content']
                else:
                    m_img = re.search(r'["\']image["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']', html, re.I)
                    if m_img: p['imageUrl'] = m_img.group(1)

            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount')
                if og_p and og_p.get('content'): p['price'] = format_price_string(og_p['content'])
                else:
                    m_p = re.search(r'["\']price["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', html)
                    if m_p: p['price'] = format_price_string(m_p.group(1))
    except: pass
    return p

def enrich_products_parallel(session, products, shop_key):
    # Alle Produkte bis zu 30 Stück anreichern
    items = [p for p in products if not p.get('imageUrl') or p.get('price') == '-'][:30]
    if items:
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(enrich_single_product, p, session, shop_key) for p in items]
            for f in futures:
                try: f.result()
                except: pass
    return products


# ===================================================================
# ISOLIERTE SHOP-ROUTINEN
# ===================================================================

# 1. AMAZON (Mit Page-2 Blätter-Funktion für garantierte 30 Ergebnisse)
def scrape_amazon(session, keyword):
    products = []
    for page in [1, 2]:
        if len(products) >= 30: break
        try:
            url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&page={page}"
            res = session.get(url, timeout=8)
            if res.status_code == 200 and "captcha" not in res.text.lower():
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for item in soup.find_all('div', {'data-component-type': 's-search-result'}):
                    if len(products) >= 30: break
                    asin = item.get('data-asin', '').strip()
                    if not asin or len(asin) != 10: continue
                    title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                    if not title_tag: continue
                   
                    item_str = str(item)
                    price = "-"
                    p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('span.a-price') or item.select_one('.a-color-price')
                    if p_elem: price = format_price_string(p_elem.get_text())
                    if price == "-":
                        p_w = item.select_one('.a-price-whole')
                        p_f = item.select_one('.a-price-fraction')
                        if p_w: price = f"{re.sub(r'[^\d]', '', p_w.get_text())},{re.sub(r'[^\d]', '', p_f.get_text()) if p_f else '00'} €"
                    if price == "-":
                        m_pr = re.search(r'(\d{1,4}[.,]\d{2})\s*€', item_str)
                        if m_pr: price = f"{m_pr.group(1).replace('.', ',')} €"

                    img_tag = item.select_one('img.s-image')
                    img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)
                    products.append({"title": title_tag.get_text().strip(), "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"})
        except: pass

    if len(products) < 30:
        valid_az = lambda u: bool(re.search(r'/(dp|gp/product)/[a-z0-9]{10}', u, re.I))
        extra = execute_external_search(session, "amazon.de/dp/", keyword, valid_az)
        for p in extra:
            m = re.search(r'/[a-z0-9]{10}', p['link'], re.I)
            if m and not p.get('imageUrl'): p['imageUrl'] = get_amazon_image_url(m.group(0).strip('/').upper())
        products.extend(extra)

    return enrich_products_parallel(session, deduplicate(products)[:30], 'amazon')


# 2. NORMA
def scrape_norma(session, keyword):
    def valid_norma(u):
        l = u.lower()
        if 'norma24.de' not in l: return False
        blocks = ['datenschutz', 'impressum', 'agb', 'kontakt', 'newsletter', 'konto', 'warenkorb', 'kategorie', 'aktionen', 'suche', 'anmelden', 'login', 'wishlist']
        if any(b in l for b in blocks): return False
        if l.rstrip('/').endswith(('norma24.de', 'norma24.de/de')): return False
        return l.count('/') >= 4

    products = []
    try:
        res = session.get(f"https://www.norma24.de/suche?q={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'norma24.de', valid_norma))
    except: pass
   
    if len(products) < 5: products.extend(execute_external_search(session, "norma24.de", keyword, valid_norma))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'norma')


# 3. NETTO
def scrape_netto(session, keyword):
    def valid_netto(u):
        l = u.lower()
        if 'netto-online.de' not in l: return False
        return any(x in l for x in ['/p-', '/p/', '/artikel/', '.html']) and '/filialen' not in l

    products = []
    try:
        res = session.get(f"https://www.netto-online.de/s/?query={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'netto-online.de', valid_netto))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "netto-online.de", keyword, valid_netto))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'netto')


# 4. KAUFLAND (Gereinigt von Wunschlisten-Noise)
def scrape_kaufland(session, keyword):
    def valid_kaufland(u):
        l = u.lower()
        if 'kaufland.de' not in l: return False
        return any(x in l for x in ['/product/', '/item/', '/pdp/'])

    products = []
    try:
        res = session.get(f"https://www.kaufland.de/item/search/?search_value={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'kaufland.de', valid_kaufland))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "kaufland.de", keyword, valid_kaufland))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'kaufland')


# 5. OTTO
def scrape_otto(session, keyword):
    def valid_otto(u):
        l = u.lower()
        if 'otto.de' not in l: return False
        return any(x in l for x in ['/p/', '#variationid=', '/pdp/'])

    products = []
    try:
        res = session.get(f"https://www.otto.de/suche/{urllib.parse.quote(keyword)}/", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'otto.de', valid_otto))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "otto.de", keyword, valid_otto))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'otto')


# 6. SMYTH TOYS
def scrape_smythtoys(session, keyword):
    def valid_smyth(u):
        l = u.lower()
        if 'smythstoys.com' not in l: return False
        return any(x in l for x in ['/p/', '/product/']) or bool(re.search(r'\d{5,}', l))

    products = []
    try:
        res = session.get(f"https://www.smythstoys.com/de/de-de/search/?text={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'smythstoys.com', valid_smyth))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "smythstoys.com", keyword, valid_smyth))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'smythtoys')


# 7. GENERISCHE SHOPS (OBI, IKEA, JYSK, H&M, DECATHLON, C&A, BAUHAUS)
def scrape_generic(session, shop_key, domain, keyword):
    def valid_generic(u):
        l = u.lower()
        if domain not in l: return False
        if l.rstrip('/').endswith(domain): return False
        return not any(b in l for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart'])

    products = execute_external_search(session, domain, keyword, valid_generic)
    return enrich_products_parallel(session, deduplicate(products)[:30], shop_key)


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

    session = get_session()
   
    if shop_key == 'amazon': products = scrape_amazon(session, keyword)
    elif shop_key == 'norma': products = scrape_norma(session, keyword)
    elif shop_key == 'netto': products = scrape_netto(session, keyword)
    elif shop_key == 'kaufland': products = scrape_kaufland(session, keyword)
    elif shop_key == 'otto': products = scrape_otto(session, keyword)
    elif shop_key == 'smythtoys': products = scrape_smythtoys(session, keyword)
    else: products = scrape_generic(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success", "shop": shop_key, "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword, "count": len(products), "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
