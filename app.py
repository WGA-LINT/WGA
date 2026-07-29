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
# NEU: INTELLIGENTER DIREKT-EXTRAKTOR (Holt Preis & Bild ohne Enricher!)
# ===================================================================
def extract_product_tiles(html, domain, url_validator):
    soup = BeautifulSoup(html, 'html.parser')
    # Lösche Header/Footer/Menüs um "Mein Konto"-Links zu vermeiden
    for tag in soup(['header', 'footer', 'nav', 'aside', 'form', 'menu']):
        tag.decompose()
       
    products = []
    seen = set()
   
    for a in soup.find_all('a', href=True):
        link = a['href']
        if link.startswith('//'): link = 'https:' + link
        elif link.startswith('/'): link = f"https://www.{domain}" + link
        link = link.split('?')[0].split('#')[0]
       
        if not url_validator(link) or link in seen: continue
       
        # Finde das Kachel-Element (Tile)
        container = a
        for _ in range(4):
            if container.parent and container.parent.name not in ['body', 'html']:
                if len(container.parent.get_text(strip=True)) < 300:
                    container = container.parent
                else: break
            else: break
               
        # 1. TITEL
        title = ""
        img = container.find('img')
        if img and img.get('alt') and len(img.get('alt')) > 8:
            title = img.get('alt')
        if not title:
            hx = container.find(['h2', 'h3', 'h4', 'h5', 'span', 'div'])
            if hx and len(hx.get_text(strip=True)) > 8 and '€' not in hx.get_text():
                title = hx.get_text(separator=' ', strip=True)
        if not title:
            title = a.get_text(separator=' ', strip=True)
           
        title = re.sub(r'\s+', ' ', title).strip()
        bad_words = ['newsletter', 'datenschutz', 'impressum', 'agb', 'wunschzettel', 'wishlist', 'anmelden', 'mein konto', 'warenkorb']
        if len(title) < 5 or any(w in title.lower() for w in bad_words): continue
           
        # 2. BILD
        img_url = ""
        for img_tag in container.find_all('img'):
            src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('srcset')
            if src:
                src = src.split(',')[0].split(' ')[0]
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
# MULTI-SUCHMASCHINEN FALLBACK (Bing, Qwant, Yahoo Kaskade)
# ===================================================================
def execute_external_search(session, domain, keyword, valid_url_func):
    products = []
    q = urllib.parse.quote(f"site:{domain} {keyword}")
   
    # 1. BING
    try:
        res = session.get(f"https://www.bing.com/search?q={q}", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for li in soup.find_all('li', class_='b_algo'):
                a = li.find('a', href=True)
                if a and valid_url_func(a['href']):
                    products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)), "price": "-", "imageUrl": "", "link": a['href'].split('?')[0]})
    except: pass
    if len(products) >= 10: return products

    # 2. QWANT
    try:
        res = session.get(f"https://lite.qwant.com/?q={q}&t=web", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.find_all('a', class_='result_url', href=True):
                link = a['href']
                if valid_url_func(link):
                    t = a.find_parent('article').find('h2').get_text(strip=True) if a.find_parent('article') else a.get_text(strip=True)
                    products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', t), "price": "-", "imageUrl": "", "link": link.split('?')[0]})
    except: pass
    if len(products) >= 10: return products
   
    # 3. YAHOO
    try:
        res = session.get(f"https://de.search.yahoo.com/search?p={q}", timeout=8)
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
# ENRICHER MIT ANTI-IP-BANN SCHUTZ
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p
    try:
        res = session.get(p['link'], timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            if not p.get('imageUrl'):
                img = soup.find('meta', property='og:image')
                if img and img.get('content'): p['imageUrl'] = img['content']
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
    # LIMITIERUNG auf 8 Artikel, um IP-Bann durch Google Sheets/Render zu verhindern!
    items = [p for p in products if not p.get('imageUrl') or p.get('price') == '-'][:8]
    if items:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(enrich_single_product, p, session, shop_key) for p in items]
            for f in futures:
                try: f.result()
                except: pass
    return products


# ===================================================================
# 100% ISOLIERTE SHOP-ROUTINEN
# ===================================================================

# 1. AMAZON (UNANGETASTET - 100% Fixiert)
def scrape_amazon(session, keyword):
    products = []
    try:
        session.get("https://www.amazon.de", timeout=4)
        res = session.get(f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss", timeout=8)
        if res.status_code == 200 and "captcha" not in res.text.lower():
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for item in soup.find_all('div', {'data-component-type': 's-search-result'}):
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

    if len(products) < 20:
        valid_az = lambda u: bool(re.search(r'/(dp|gp/product)/[a-z0-9]{10}', u, re.I))
        products.extend(execute_external_search(session, "amazon.de/dp/", keyword, valid_az))
        for p in products:
            m = re.search(r'/[a-z0-9]{10}', p['link'], re.I)
            if m and not p.get('imageUrl'): p['imageUrl'] = get_amazon_image_url(m.group(0).strip('/').upper())

    return enrich_products_parallel(session, deduplicate(products)[:30], 'amazon')


# 2. NORMA (Extrem strenger Filter + Extraktor)
def scrape_norma(session, keyword):
    def valid_norma(u):
        l = u.lower()
        if 'norma24.de' not in l: return False
        blocks = ['datenschutz', 'impressum', 'agb', 'kontakt', 'newsletter', 'konto', 'warenkorb', 'kategorie', 'aktionen', 'suche', 'anmelden', 'login', 'wishlist', 'wunschzettel']
        if any(b in l for b in blocks): return False
        if l.rstrip('/').endswith(('norma24.de', 'norma24.de/de')): return False
        if l.count('/') < 4: return False
        return True

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
        if not any(x in l for x in ['/p-', '/p/', '/artikel/', '.html']): return False
        return True

    products = []
    try:
        res = session.get(f"https://www.netto-online.de/s/?query={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'netto-online.de', valid_netto))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "netto-online.de", keyword, valid_netto))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'netto')


# 4. KAUFLAND
def scrape_kaufland(session, keyword):
    def valid_kaufland(u):
        if 'kaufland.de' not in u.lower(): return False
        if not any(x in u.lower() for x in ['/product/', '/item/', '/pdp/']): return False
        return True

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
        if 'otto.de' not in u.lower(): return False
        if not any(x in u.lower() for x in ['/p/', '#variationid=', '/pdp/']): return False
        return True

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
        if 'smythstoys.com' not in u.lower(): return False
        if not any(x in u.lower() for x in ['/p/', '/product/']) and not re.search(r'\d{5,}', u): return False
        return True

    products = []
    try:
        res = session.get(f"https://www.smythstoys.com/de/de-de/search/?text={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products.extend(extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'smythstoys.com', valid_smyth))
    except: pass

    if len(products) < 5: products.extend(execute_external_search(session, "smythstoys.com", keyword, valid_smyth))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'smythtoys')


# 7. GENERISCHE SHOPS (OBI, IKEA, JYSK, ETC.)
def scrape_generic(session, shop_key, domain, keyword):
    def valid_generic(u):
        if domain not in u.lower(): return False
        if u.rstrip('/').endswith(domain): return False
        if '/impressum' in u.lower() or '/datenschutz' in u.lower(): return False
        return True

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
