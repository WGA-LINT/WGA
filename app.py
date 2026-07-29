import os
import re
import urllib.parse
import time
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
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Upgrade-Insecure-Requests': '1'
}

MAX_RESULTS = 50

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
# UNIVERSAL-EXTRAKTOR
# ===================================================================
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
            if alt and len(alt) > 6 and not any(bad in alt.lower() for bad in ['logo', 'herz', 'wishlist', 'icon']):
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
            src = (img_tag.get('src') or img_tag.get('srcset') or img_tag.get('data-src') or '')
            if src:
                src = src.split(',')[0].split(' ')[0].strip()
                if not any(bad in src.lower() for bad in ['heart', 'herz', 'icon', 'placeholder', '.svg']):
                    if src.startswith('//'): src = 'https:' + src
                    elif src.startswith('/'): src = f"https://www.{domain}" + src
                    if src.startswith('http') and not src.startswith('data:image'):
                        img_url = src
                        break
                   
        price = "-"
        m = re.search(r'(\d{1,4}[.,]\d{2})\s*€?', container.get_text(separator=' '))
        if m: price = format_price_string(m.group(1))
           
        products.append({"title": title, "price": price, "imageUrl": img_url, "link": link})
        seen.add(link)
       
    return products


# ===================================================================
# NEUER AGGRESSIVER FALLBACK (DuckDuckGo + Bing)
# ===================================================================
def execute_external_search(session, domain, keyword, valid_url_func):
    products, seen = [], set()
    q = f"site:{domain} {keyword}"

    # 1. DuckDuckGo (HTML Version - Sehr Scraper-freundlich)
    try:
        res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content, 'html.parser')
            for a in soup.find_all('a', class_='result__url'):
                href = a.get('href', '')
                if 'uddg=' in href:
                    href = urllib.parse.unquote(href.split('uddg=')[1].split('&')[0])
               
                if valid_url_func(href) and href not in seen:
                    container = a.find_parent('div', class_='result')
                    title_elem = container.find('h2', class_='result__title') if container else None
                    title = title_elem.get_text(strip=True) if title_elem else a.get_text(strip=True)
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                   
                    if len(title) > 5 and not title.lower().startswith('http'):
                        products.append({"title": title, "price": "-", "imageUrl": "", "link": href})
                        seen.add(href)
    except: pass

    if len(products) >= MAX_RESULTS: return products[:MAX_RESULTS]

    # 2. Bing (Backup)
    try:
        res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.find_all('a', href=True):
                link = a['href'].split('?')[0]
                if valid_url_func(link) and link not in seen:
                    title = a.get_text(strip=True)
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                    if len(title) > 5 and not title.lower().startswith('http'):
                        products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
                        seen.add(link)
    except: pass

    return products[:MAX_RESULTS]


# ===================================================================
# ENRICHER (Mit Otto-Bremse & Spezial-Scannern für OBI/JYSK)
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p
    try:
        res = session.get(p['link'], timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # Preis auslesen (Generisch)
            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'): p['price'] = format_price_string(og_p['content'])
                else:
                    m_p = re.search(r'["\'](?:price|amount)["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', html)
                    if m_p: p['price'] = format_price_string(m_p.group(1))

            # Bild auslesen (Generisch)
            if not p.get('imageUrl'):
                img = soup.find('meta', property='og:image')
                if img and img.get('content'): p['imageUrl'] = img['content']

            # --- SPEZIAL-FIXES FÜR FEHLENDE BILDER/PREISE ---
           
            # Otto (Preis aus JSON oder data-qa)
            if shop_key == 'otto' and p.get('price') == '-':
                m_otto = re.search(r'data-qa=["\']price["\'][^>]*>([^<]+)', html)
                if m_otto: p['price'] = format_price_string(m_otto.group(1))

            # OBI (Bilder versteckt in speziellem img.obi.de CDN)
            if shop_key == 'obi' and not p.get('imageUrl'):
                m_obi = re.search(r'["\'](https://img\.obi\.de/[^"\']+)["\']', html)
                if m_obi: p['imageUrl'] = m_obi.group(1).replace('\\u002F', '/')

            # JYSK (Bilder in cdn-Struktur)
            if shop_key == 'jysk' and not p.get('imageUrl'):
                m_jysk = re.search(r'["\'](https://cdn[^"\']*jysk[^"\']*\.(?:jpg|png|webp))["\']', html)
                if m_jysk: p['imageUrl'] = m_jysk.group(1).replace('\\u002F', '/')
    except: pass
    return p

def enrich_products_parallel(session, products, shop_key):
    items = [p for p in products if not p.get('imageUrl') or p.get('price') == '-'][:MAX_RESULTS]
    if items:
        # OTTO FIX: Drosselung auf max_workers=2, damit Otto (Rate Limit) nicht blockt!
        workers = 2 if shop_key == 'otto' else 6
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(enrich_single_product, p, session, shop_key) for p in items]
            for f in futures:
                try: f.result()
                except: pass
    return products


# ===================================================================
# ISOLIERTE SHOP-ROUTINEN
# ===================================================================

# 1. AMAZON (Gefroren - Funktioniert perfekt)
def scrape_amazon(session, keyword):
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
                    img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)
                    products.append({"title": title_tag.get_text().strip(), "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"})
        except: pass
    return deduplicate(products)[:MAX_RESULTS]


# 2. KAUFLAND (Gefroren - Funktioniert perfekt)
def scrape_kaufland(session, keyword):
    def valid_kaufland(u):
        l = u.lower()
        if 'kaufland.de' not in l: return False
        return '/product/' in l or '/item/' in l

    products = []
    try:
        res = session.get(f"https://www.kaufland.de/s/?search_value={urllib.parse.quote(keyword)}&sort=relevance", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'kaufland.de', valid_kaufland)
    except: pass
    return deduplicate(products)[:MAX_RESULTS]


# 3. BAUHAUS (Gefroren - Funktioniert perfekt)
def scrape_bauhaus(session, keyword):
    def valid_bauhaus(u):
        l = u.lower()
        if 'bauhaus.info' not in l: return False
        return '/p/' in l or '/produkt' in l or re.search(r'/\d{7,}', l)

    products = []
    try:
        res = session.get(f"https://www.bauhaus.info/suche/produkte?q={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'bauhaus.info', valid_bauhaus)
    except: pass
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'bauhaus')


# 4. OTTO (Funktioniert, nutzt aber jetzt den gebremsten Enricher für alle 50 Produkte)
def scrape_otto(session, keyword):
    def valid_otto(u):
        l = u.lower()
        if 'otto.de' not in l: return False
        return '/p/' in l or '#variationid=' in l

    products = []
    try:
        res = session.get(f"https://www.otto.de/suche/{urllib.parse.quote(keyword)}/?sort=bestseller", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'otto.de', valid_otto)
    except: pass
    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'otto')


# 5. JYSK (Rigoroser URL-Filter, damit Kategorieseiten wie Betten ignoriert werden)
def scrape_jysk(session, keyword):
    def valid_jysk(u):
        l = u.lower()
        if 'jysk.de' not in l: return False
        # EXTREMER FILTER: Akzeptiert nur Links mit Artikelnummern (z.B. p-12345, -p12345)
        if not re.search(r'(-p-?\d+|/p-?\d+|\d{6,}\.html?)', l): return False
        return True

    products = []
    try:
        res = session.get(f"https://jysk.de/search?query={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'jysk.de', valid_jysk)
    except: pass

    if len(products) < 10:
        products.extend(execute_external_search(session, "jysk.de", keyword, valid_jysk))

    return enrich_products_parallel(session, deduplicate(products)[:MAX_RESULTS], 'jysk')


# 6. GENERISCHE SHOPS (Norma, Netto, OBI, H&M, IKEA, Smyths, Decathlon, C&A)
def scrape_generic(session, shop_key, domain, keyword):
    search_urls = {
        'obi': f"https://www.obi.de/search/{urllib.parse.quote(keyword)}/?sort=relevance",
        'hm': f"https://www2.hm.com/de_de/search-results.html?q={urllib.parse.quote(keyword)}",
        'ikea': f"https://www.ikea.com/de/de/search/products/?q={urllib.parse.quote(keyword)}",
        'decathlon': f"https://www.decathlon.de/search?Ntt={urllib.parse.quote(keyword)}",
        'cna': f"https://www.c-and-a.com/de/de/shop/search?q={urllib.parse.quote(keyword)}",
        'norma': f"https://www.norma24.de/suche?q={urllib.parse.quote(keyword)}",
        'netto': f"https://www.netto-online.de/s/?query={urllib.parse.quote(keyword)}",
        'smythtoys': f"https://www.smythstoys.com/de/de-de/search/?text={urllib.parse.quote(keyword)}"
    }

    def valid_generic(u):
        l = u.lower()
        if domain not in l: return False
        if l.rstrip('/').endswith(domain): return False
        if any(b in l for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto']): return False
        if shop_key == 'hm' and not '/productpage.' in l: return False
        if shop_key == 'ikea' and not '/p/' in l: return False
        return True

    products = []
    url = search_urls.get(shop_key, f"https://www.{domain}/suche?q={urllib.parse.quote(keyword)}")
   
    # Versuch Direktsuche
    try:
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, valid_generic)
    except: pass

    # Aggressiver DuckDuckGo/Bing Fallback, wenn Seite JS-Blockade hat
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

    session = get_session()
   
    if shop_key == 'amazon': products = scrape_amazon(session, keyword)
    elif shop_key == 'kaufland': products = scrape_kaufland(session, keyword)
    elif shop_key == 'bauhaus': products = scrape_bauhaus(session, keyword)
    elif shop_key == 'otto': products = scrape_otto(session, keyword)
    elif shop_key == 'jysk': products = scrape_jysk(session, keyword)
    else: products = scrape_generic(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success", "shop": shop_key, "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword, "count": len(products), "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
