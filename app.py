from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
import re
import json
import urllib.parse
from curl_cffi import requests as crequests

app = Flask(__name__)

# Vollständige Browser-Browser-Signatur
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Connection': 'keep-alive',
    'Upgrade-Insecure-Requests': '1',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1'
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

def create_session():
    """Erstellt eine hochresistente Requests-Session mit Browser-Impersonation"""
    session = crequests.Session(impersonate="chrome124")
    session.headers.update(HEADERS)
    return session

@app.route('/')
def home():
    return jsonify({
        "status": "online",
        "engine": "Ultra-Resilient Hybrid Engine v3.0",
        "supported_shops": list(SHOP_DOMAINS.keys())
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
    session = create_session()
    products = []

    # --- STRATEGIE 1: Direktes Shop-Scraping & JSON-LD Extraction ---
    try:
        if shop_key == 'amazon':
            products = scrape_amazon_direct(session, keyword)
        elif shop_key == 'otto':
            products = scrape_otto_direct(session, keyword)
    except Exception as e:
        print(f"Direct scrape failed for {shop_key}: {e}")

    # --- STRATEGIE 2: DuckDuckGo Lite Engine (Search Aggregator) ---
    if len(products) < 5:
        try:
            ddg_prods = scrape_ddg_lite(session, target_domain, keyword)
            products.extend(ddg_prods)
            products = deduplicate_products(products)
        except Exception as e:
            print(f"DDG Lite failed: {e}")

    # --- STRATEGIE 3: Bing Multi-Index Fallback ---
    if len(products) < 5:
        try:
            bing_prods = scrape_bing_search(session, target_domain, keyword)
            products.extend(bing_prods)
            products = deduplicate_products(products)
        except Exception as e:
            print(f"Bing failed: {e}")

    # Nachbearbeitung: Bild- & URL-Normalisierung für alle Shops
    products = normalize_and_enrich_products(products, shop_key, target_domain)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products[:30]
    })


# ==========================================
# DIREKTE SHOP-ENGINES (JSON-LD & DOM)
# ==========================================

def scrape_amazon_direct(session, keyword):
    """Direkte Amazon-Suche mit Selektor-Fallback"""
    products = []
    url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}"
    res = session.get(url, timeout=12)
   
    if res.status_code != 200 or "api-services-support@amazon.com" in res.text:
        return products

    soup = BeautifulSoup(res.text, 'lxml')
    items = soup.select('div[data-component-type="s-search-result"]')

    for item in items:
        try:
            asin = item.get('data-asin', '')
            title_el = item.select_one('h2 a span') or item.select_one('h2')
            link_el = item.select_one('h2 a')

            if not title_el or not asin:
                continue

            title = title_el.get_text().strip()
            link = f"https://www.amazon.de/dp/{asin}"
            img_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

            # Preiserfassung
            price = "-"
            p_whole = item.select_one('.a-price-whole')
            p_frac = item.select_one('.a-price-fraction')
            if p_whole:
                w_txt = p_whole.get_text().replace('.', '').strip()
                f_txt = p_frac.get_text().strip() if p_frac else "00"
                price = f"{w_txt},{f_txt} €"

            products.append({
                "title": title,
                "price": price,
                "imageUrl": img_url,
                "link": link
            })
        except Exception:
            continue
           
    return products


def scrape_otto_direct(session, keyword):
    """Parst OTTO.de direkt über eingebettete JSON-LD Daten"""
    products = []
    url = f"https://www.otto.de/suche/{urllib.parse.quote(keyword)}"
    res = session.get(url, timeout=12)
   
    if res.status_code != 200:
        return products

    soup = BeautifulSoup(res.text, 'lxml')
   
    # Heuristik: Suche nach Schema.org JSON-LD
    scripts = soup.find_all('script', type='application/ld+json')
    for script in scripts:
        try:
            data = json.loads(script.string or '{}')
            if isinstance(data, dict) and data.get('@type') == 'ItemList':
                elements = data.get('itemListElement', [])
                for elem in elements:
                    item = elem.get('item', {})
                    if item:
                        title = item.get('name', '')
                        link = item.get('url', '')
                        offers = item.get('offers', {})
                        price = f"{offers.get('price', '-')} €" if isinstance(offers, dict) and 'price' in offers else "-"
                        img = item.get('image', '')
                        if isinstance(img, list) and len(img) > 0:
                            img = img[0]

                        if title and link:
                            products.append({
                                "title": title,
                                "price": price,
                                "imageUrl": img if isinstance(img, str) else "",
                                "link": link if link.startswith('http') else f"https://www.otto.de{link}"
                            })
        except Exception:
            continue

    return products


# ==========================================
# SEARCH ENGINE FALLBACKS (UNIVERSAL)
# ==========================================

def scrape_ddg_lite(session, domain, keyword):
    products = []
    url = "https://lite.duckduckgo.com/lite/"
    data = {'q': f'site:{domain} {keyword}'}

    res = session.post(url, data=data, timeout=10)
    if res.status_code != 200:
        return products

    soup = BeautifulSoup(res.text, 'lxml')
    rows = soup.find_all('tr')

    for i in range(len(rows)):
        a = rows[i].find('a', class_='result-link')
        if not a:
            continue

        raw_href = a.get('href', '')
        link = urllib.parse.unquote(raw_href.split('uddg=')[1].split('&')[0]) if 'uddg=' in raw_href else raw_href

        if not is_valid_product_url(link, domain):
            continue

        clean_link = link.split('?')[0]
        title = clean_product_title(a.get_text(), domain)
        if not title:
            continue

        # Preis-Extraktion aus Text-Snippet
        price = "-"
        if i + 1 < len(rows):
            snippet_td = rows[i + 1].find('td', class_='result-snippet')
            if snippet_td:
                price = extract_price(snippet_td.get_text())

        products.append({
            "title": title,
            "price": price,
            "imageUrl": "",
            "link": clean_link
        })

    return products


def scrape_bing_search(session, domain, keyword):
    products = []
    url = f"https://www.bing.com/search?q=site:{domain}+{urllib.parse.quote(keyword)}"
   
    res = session.get(url, timeout=10)
    if res.status_code != 200:
        return products

    soup = BeautifulSoup(res.text, 'lxml')
    items = soup.select('li.b_algo')

    for item in items:
        a = item.find('a', href=True)
        if not a:
            continue

        link = a['href']
        if not is_valid_product_url(link, domain):
            continue

        clean_link = link.split('?')[0]
        title = clean_product_title(a.get_text(), domain)
        if not title:
            continue

        price = extract_price(item.get_text())
        products.append({
            "title": title,
            "price": price,
            "imageUrl": "",
            "link": clean_link
        })

    return products


# ==========================================
# HEURISTISCHE PARSER & FILTER
# ==========================================

def is_valid_product_url(url, domain):
    u = url.lower()
    if domain not in u:
        return False
   
    # Ausschluss von Service-/Systemseiten
    bad_paths = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/presse', '/kontakt', '/konto']
    if any(b in u for b in bad_paths):
        return False
       
    return True


def clean_product_title(title, domain):
    if not title:
        return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'(online kaufen|jetzt bestellen|bei Amazon|auf OTTO\.de|im Shop).*$', '', clean, flags=re.IGNORECASE)
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
        identifier = p['link'].lower()
        if identifier not in seen:
            seen.add(identifier)
            unique.append(p)
    return unique


def normalize_and_enrich_products(products, shop_key, domain):
    for p in products:
        # ASIN-Extraktion für Amazon
        if shop_key == 'amazon' or 'amazon.de' in p['link']:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'])
            if asin_match:
                asin = asin_match.group(1)
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                p['imageUrl'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

        # Universeller Bild-Fallback (falls kein Bild vorhanden)
        if not p.get('imageUrl'):
            p['imageUrl'] = ""

    return products
