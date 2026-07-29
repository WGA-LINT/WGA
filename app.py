from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
import re
import urllib.parse

try:
    from curl_cffi import requests as crequests
    USE_CURL = True
except ImportError:
    import requests as crequests
    USE_CURL = False

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
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

def fetch_html(url, method="GET", data=None):
    try:
        if USE_CURL:
            if method == "POST":
                return crequests.post(url, data=data, headers=HEADERS, impersonate="chrome120", timeout=12)
            return crequests.get(url, headers=HEADERS, impersonate="chrome120", timeout=12)
        else:
            if method == "POST":
                return crequests.post(url, data=data, headers=HEADERS, timeout=12)
            return crequests.get(url, headers=HEADERS, timeout=12)
    except Exception as e:
        print(f"Fetch Error ({url}): {e}")
        return None

@app.route('/')
def home():
    return jsonify({"status": "online", "curl_cffi": USE_CURL})

@app.route('/debug')
def debug():
    shop = request.args.get('shop', 'amazon')
    keyword = request.args.get('keyword', 'katzenspielzeug')
   
    # Test DDG Lite
    ddg_products = search_ddg_lite(SHOP_DOMAINS.get(shop, 'amazon.de'), keyword)
   
    # Test Direct Search
    direct_products = []
    if shop == 'amazon':
        direct_products = direct_amazon_search(keyword)
       
    return jsonify({
        "shop": shop,
        "keyword": keyword,
        "ddg_lite_count": len(ddg_products),
        "direct_count": len(direct_products),
        "sample_ddg": ddg_products[:2],
        "sample_direct": direct_products[:2]
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

    # Strategie 1: Direkt-Suche (falls Amazon)
    if shop_key == 'amazon':
        products = direct_amazon_search(keyword)

    # Strategie 2: DuckDuckGo Lite Search (Cloud-IP-resistent)
    if len(products) < 5:
        ddg_prods = search_ddg_lite(target_domain, keyword)
        products.extend(ddg_prods)
        products = remove_duplicates(products)

    # Nachbearbeitung Amazon-Bilder
    if shop_key == 'amazon':
        for p in products:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'])
            if asin_match:
                asin = asin_match.group(1)
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                if not p['imageUrl'] or 'amazon' not in p['imageUrl']:
                    p['imageUrl'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products
    })


def direct_amazon_search(keyword):
    """Direkte Suche auf Amazon.de mit TLS-Impersonation"""
    products = []
    url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}"
    res = fetch_html(url)
   
    if not res or res.status_code != 200:
        return products

    soup = BeautifulSoup(res.text, 'html.parser')
    items = soup.select('div[data-component-type="s-search-result"]')

    for item in items:
        try:
            title_tag = item.select_one('h2 a span')
            link_tag = item.select_one('h2 a')
            price_whole = item.select_one('.a-price-whole')
            price_fraction = item.select_one('.a-price-fraction')
            img_tag = item.select_one('img.s-image')

            if not title_tag or not link_tag:
                continue

            title = title_tag.get_text().strip()
            raw_link = link_tag.get('href', '')
           
            asin = item.get('data-asin', '')
            if asin:
                link = f"https://www.amazon.de/dp/{asin}"
                img_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"
            else:
                link = "https://www.amazon.de" + raw_link.split('?')[0]
                img_url = img_tag.get('src', '') if img_tag else ""

            price = "-"
            if price_whole:
                p_w = price_whole.get_text().replace('.', '').strip()
                p_f = price_fraction.get_text().strip() if price_fraction else "00"
                price = f"{p_w},{p_f} €"

            products.append({
                "title": title,
                "price": price,
                "imageUrl": img_url,
                "link": link
            })
            if len(products) >= 30:
                break
        except Exception:
            continue

    return products


def search_ddg_lite(domain, keyword):
    """DuckDuckGo Lite (Sehr verlässlich gegen Datacenter-Blocks)"""
    products = []
    seen = set()
    url = "https://lite.duckduckgo.com/lite/"
    data = {'q': f'site:{domain} {keyword}'}

    res = fetch_html(url, method="POST", data=data)
    if not res or res.status_code != 200:
        return products

    soup = BeautifulSoup(res.text, 'html.parser')
    rows = soup.find_all('tr')

    for i in range(len(rows)):
        a = rows[i].find('a', class_='result-link')
        if not a:
            continue

        raw_href = a.get('href', '')
        if 'uddg=' in raw_href:
            link = urllib.parse.unquote(raw_href.split('uddg=')[1].split('&')[0])
        else:
            link = raw_href

        if not is_valid_product_url(link, domain):
            continue

        clean_link = link.split('?')[0]
        if clean_link in seen:
            continue

        title = clean_product_title(a.get_text(), domain)
        if not title or len(title) < 3:
            continue

        seen.add(clean_link)

        # Preis aus dem Textauszug (Snippet) der Folgezeile auslesen
        price = "-"
        if i + 1 < len(rows):
            snippet_td = rows[i + 1].find('td', class_='result-snippet')
            if snippet_td:
                price = extract_price_from_text(snippet_td.get_text())

        products.append({
            "title": title,
            "price": price,
            "imageUrl": "",
            "link": clean_link
        })

        if len(products) >= 30:
            break

    return products


def is_valid_product_url(url, domain):
    u = url.lower()
    if domain not in u:
        return False
    bad = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/kontakt']
    for b in bad:
        if b in u:
            return False
    if 'amazon.de' in domain and not ('/dp/' in u or '/gp/product/' in u):
        return False
    return True


def clean_product_title(title, domain):
    if not title:
        return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'online kaufen.*$', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'bei Amazon.*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_price_from_text(text):
    if not text:
        return "-"
    match = re.search(r'(\d{1,4}[.,]\d{2})\s*€', text) or re.search(r'€\s*(\d{1,4}[.,]\d{2})', text)
    if match:
        val = match.group(1).replace('.', ',')
        return val if '€' in val else f"{val} €"
    return "-"


def remove_duplicates(products):
    seen = set()
    unique = []
    for p in products:
        if p['title'] not in seen:
            seen.add(p['title'])
            unique.append(p)
    return unique
