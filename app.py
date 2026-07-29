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
# UNIVERSAL-EXTRAKTOR (Gefixt: Verwirft keine echten Produkte mehr)
# ===================================================================
def extract_product_tiles(html, domain, url_validator):
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['header', 'footer', 'nav', 'aside', 'menu']):
        tag.decompose()
       
    products = []
    seen = set()

    for a in soup.find_all('a', href=True):
        link = a['href']
        if link.startswith('//'): link = 'https:' + link
        elif link.startswith('/'): link = f"https://www.{domain}" + link
        link = link.split('?')[0].split('#')[0]
       
        if not url_validator(link) or link in seen: continue
       
        container = a
        for _ in range(3):
            if container.parent and container.parent.name not in ['body', 'html']:
                if len(container.parent.get_text(strip=True)) < 500:
                    container = container.parent
                else: break
            else: break
               
        # 1. TITEL
        title = ""
        for img_cand in container.find_all('img'):
            alt = img_cand.get('alt', '').strip()
            if alt and len(alt) > 6 and not any(bad in alt.lower() for bad in ['logo', 'herz', 'wishlist', 'icon', 'stern']):
                title = alt
                break
               
        if not title:
            hx = container.find(['h1', 'h2', 'h3', 'h4', 'h5'])
            if hx and len(hx.get_text(strip=True)) > 5:
                title = hx.get_text(separator=' ', strip=True)
               
        if not title:
            title = a.get_text(separator=' ', strip=True)
           
        title = re.sub(r'\s+', ' ', title).strip()
        # Säubere Titel von Button-Texten
        title = re.sub(r'(In den Warenkorb|Auf die Wunschliste|Merken|Hinzufügen).*$', '', title, flags=re.I).strip()
       
        if len(title) < 4 or title.lower() in ['login', 'warenkorb', 'suche', 'menu', 'konto', 'datenschutz', 'impressum']:
            continue
           
        # 2. BILD (Inklusive Lazy-Loading)
        img_url = ""
        for img_tag in container.find_all('img'):
            src = (img_tag.get('src') or img_tag.get('data-src') or
                   img_tag.get('data-srcset') or img_tag.get('data-original') or
                   img_tag.get('srcset') or '')
            if src:
                src = src.split(',')[0].split(' ')[0].strip()
                if any(bad_img in src.lower() for bad_img in ['heart', 'herz', 'icon', 'wishlist', 'placeholder', '.svg']):
                    continue
                if src.startswith('//'): src = 'https:' + src
                elif src.startswith('/'): src = f"https://www.{domain}" + src
                if src.startswith('http') and not src.startswith('data:image'):
                    img_url = src
                    break
                   
        # 3. PREIS
        price = "-"
        m = re.search(r'(\d{1,4}[.,]\d{2})\s*€?', container.get_text(separator=' '))
        if m: price = format_price_string(m.group(1))
           
        products.append({"title": title, "price": price, "imageUrl": img_url, "link": link})
        seen.add(link)
       
    return products


# ===================================================================
# DIREKT-SUCH-ROUTINEN PRO SHOP
# ===================================================================

# 1. AMAZON (Zu 100% unverändert & stabil)
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

    return deduplicate(products)[:30]


# 2. NORMA
def scrape_norma(session, keyword):
    def valid_norma(u):
        l = u.lower()
        if 'norma24.de' not in l: return False
        blocks = ['/datenschutz', '/impressum', '/agb', '/kontakt', '/newsletter', '/konto', '/warenkorb', '/anmelden', '/login']
        if any(b in l for b in blocks): return False
        return l.rstrip('/') != 'https://www.norma24.de' and l.rstrip('/') != 'https://www.norma24.de/de'

    products = []
    try:
        res = session.get(f"https://www.norma24.de/suche?q={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'norma24.de', valid_norma)
    except: pass
    return deduplicate(products)[:30]


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
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'netto-online.de', valid_netto)
    except: pass
    return deduplicate(products)[:30]


# 4. KAUFLAND
def scrape_kaufland(session, keyword):
    def valid_kaufland(u):
        l = u.lower()
        if 'kaufland.de' not in l: return False
        return any(x in l for x in ['/product/', '/item/', '/pdp/'])

    products = []
    try:
        res = session.get(f"https://www.kaufland.de/s/?search_value={urllib.parse.quote(keyword)}", timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'kaufland.de', valid_kaufland)
    except: pass
    return deduplicate(products)[:30]


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
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'otto.de', valid_otto)
    except: pass
    return deduplicate(products)[:30]


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
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), 'smythstoys.com', valid_smyth)
    except: pass
    return deduplicate(products)[:30]


# 7. GENERISCHE DIREKT-SUCHEN (OBI, IKEA, JYSK, H&M, DECATHLON, C&A, BAUHAUS)
def scrape_generic(session, shop_key, domain, keyword):
    search_urls = {
        'obi': f"https://www.obi.de/search/{urllib.parse.quote(keyword)}/",
        'hm': f"https://www2.hm.com/de_de/search-results.html?q={urllib.parse.quote(keyword)}",
        'ikea': f"https://www.ikea.com/de/de/search/?q={urllib.parse.quote(keyword)}",
        'jysk': f"https://jysk.de/search?query={urllib.parse.quote(keyword)}",
        'decathlon': f"https://www.decathlon.de/search?Ntt={urllib.parse.quote(keyword)}",
        'cna': f"https://www.c-and-a.com/de/de/shop/search?q={urllib.parse.quote(keyword)}",
        'bauhaus': f"https://www.bauhaus.info/suche/produkte?q={urllib.parse.quote(keyword)}"
    }

    def valid_generic(u):
        l = u.lower()
        if domain not in l: return False
        if l.rstrip('/').endswith(domain): return False
        return not any(b in l for b in ['/impressum', '/datenschutz', '/agb', '/login', '/cart', '/konto', '/service'])

    products = []
    url = search_urls.get(shop_key, f"https://www.{domain}/suche?q={urllib.parse.quote(keyword)}")
    try:
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            products = extract_product_tiles(res.content.decode('utf-8', 'ignore'), domain, valid_generic)
    except: pass

    return deduplicate(products)[:30]


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
