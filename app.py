from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
import re
import urllib.parse

# 1. DuckDuckGo Engine (Handles vqd-Token & Datacenter-Bypass)
try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

# 2. TLS Impersonation via curl_cffi
try:
    from curl_cffi import requests as crequests
    HAS_CURL = True
except ImportError:
    import requests as crequests
    HAS_CURL = False

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

SHOP_DOMAINS = {
    'amazon': 'amazon.de',
    'norma': 'norma24.de',
    'netto': 'netto-online.de',
    'obi': 'obi.de',
    'hm': 'hm.com',
    'ikea': 'ikea.com',
    'jysk': 'jysk.de',
    'kaufland': 'kaufland.de',
    'otto': 'otto.de',
    'smythtoys': 'smythstoys.com',
    'decathlon': 'decathlon.de',
    'cna': 'c-and-a.com',
    'bauhaus': 'bauhaus.info'
}

def make_session():
    if HAS_CURL:
        session = crequests.Session(impersonate="chrome120")
    else:
        session = crequests.Session()
    session.headers.update(HEADERS)
    return session

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "ddgs_available": HAS_DDGS,
        "curl_cffi_available": HAS_CURL,
        "shops": list(SHOP_DOMAINS.keys())
    })

@app.route('/scrape', methods=['GET'])
def scrape():
    shop_key = request.args.get('shop', '').lower().strip()
    keyword = request.args.get('keyword', '').strip()

    if not keyword or not shop_key:
        return jsonify({"error": "Parameter 'shop' und 'keyword' erforderlich."}), 400

    if shop_key not in SHOP_DOMAINS:
        return jsonify({"error": f"Unbekannter Shop '{shop_key}'."}), 400

    target_domain = SHOP_DOMAINS[shop_key]
    products = []

    # Stufe 1: DuckDuckGo Native API (Am verlässlichsten auf Datacenter-IPs)
    if HAS_DDGS:
        products = search_ddgs(target_domain, keyword)

    # Stufe 2: Google Search Fallback mit TLS Impersonation
    if len(products) < 5:
        session = make_session()
        google_prods = search_google(session, target_domain, keyword)
        products.extend(google_prods)
        products = deduplicate_products(products)

    # Stufe 3: Yahoo Multi-Search Fallback
    if len(products) < 5:
        session = make_session()
        yahoo_prods = search_yahoo(session, target_domain, keyword)
        products.extend(yahoo_prods)
        products = deduplicate_products(products)

    # Nachbearbeitung: Amazon ASIN-Extraktion & Bild-Generierung
    products = enrich_products(products, shop_key, target_domain)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products[:30]
    })


def search_ddgs(domain, keyword):
    """Sucht über die offizielle DuckDuckGo-Python-Engine mit Token-Handshake"""
    products = []
    query = f"site:{domain} {keyword}"
    try:
        results = list(DDGS().text(query, region="de-de", max_results=35))
        for r in results:
            link = r.get('href', '')
            title = r.get('title', '')
            snippet = r.get('body', '')

            if not is_valid_product_url(link, domain):
                continue

            clean_title = clean_product_title(title, domain)
            if not clean_title:
                continue

            price = extract_price(snippet)

            products.append({
                "title": clean_title,
                "price": price,
                "imageUrl": "",
                "link": link.split('?')[0]
            })
    except Exception as e:
        print(f"DDGS Engine Error: {e}")
    return products


def search_google(session, domain, keyword):
    """Sucht über Google Search mit Chrome-TLS-Signaturen"""
    products = []
    url = f"https://www.google.de/search?q=site:{domain}+{urllib.parse.quote(keyword)}&num=30&hl=de"
    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'lxml')
            for g in soup.select('div.g'):
                a = g.find('a', href=True)
                h3 = g.find('h3')
                if not a or not h3:
                    continue

                link = a['href']
                if not is_valid_product_url(link, domain):
                    continue

                title = clean_product_title(h3.get_text(), domain)
                if not title:
                    continue

                snippet_el = g.select_one('div.VwiC3b') or g
                price = extract_price(snippet_el.get_text())

                products.append({
                    "title": title,
                    "price": price,
                    "imageUrl": "",
                    "link": link.split('?')[0]
                })
    except Exception as e:
        print(f"Google Search Error: {e}")
        return products
    return products


def search_yahoo(session, domain, keyword):
    """Sucht über Yahoo Search als dritter Absicherungs-Layer"""
    products = []
    url = f"https://search.yahoo.com/search?p=site:{domain}+{urllib.parse.quote(keyword)}"
    try:
        res = session.get(url, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'lxml')
            for div in soup.find_all('div', class_='compTitle'):
                a = div.find('a', href=True)
                if not a:
                    continue
                link = a['href']
                if not is_valid_product_url(link, domain):
                    continue

                title = clean_product_title(a.get_text(), domain)
                if not title:
                    continue

                parent = div.find_parent('div', class_='dd')
                price = extract_price(parent.get_text()) if parent else "-"

                products.append({
                    "title": title,
                    "price": price,
                    "imageUrl": "",
                    "link": link.split('?')[0]
                })
    except Exception as e:
        print(f"Yahoo Search Error: {e}")
    return products


def is_valid_product_url(url, domain):
    u = url.lower()
    if domain not in u:
        return False

    bad_paths = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/presse', '/kontakt', '/konto']
    if any(b in u for b in bad_paths):
        return False

    if 'amazon.de' in domain and not ('/dp/' in u or '/gp/product/' in u or '/asin/' in u):
        return False

    return True


def clean_product_title(title, domain):
    if not title:
        return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'(online kaufen|jetzt bestellen|bei Amazon|auf OTTO\.de|im Shop|kaufen bei).*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_price(text):
    if not text:
        return "-"
    match = re.search(r'(\d{1,4}[.,]\d{2})\s*€', text) or re.search(r'€\s*(\d{1,4}[.,]\d{2})', text)
    if match:
        val = match.group(1).replace('.', ',')
        return val if '€' in val else f"{val} €"
    return "-"


def deduplicate_products(products):
    seen = set()
    unique = []
    for p in products:
        ident = p['link'].lower()
        if ident not in seen:
            seen.add(ident)
            unique.append(p)
    return unique


def enrich_products(products, shop_key, domain):
    """Reichert Links und Bilder universell an"""
    for p in products:
        # Amazon Synthese: Aus der ASIN im Link wird das Bild & der saubere Link gebaut
        if shop_key == 'amazon' or 'amazon.de' in p['link']:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'])
            if asin_match:
                asin = asin_match.group(1)
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                p['imageUrl'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

    return products
