import os
import re
import urllib.parse
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
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'Sec-Ch-Ua': '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'document',
    'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'none',
    'Sec-Fetch-User': '?1',
    'Upgrade-Insecure-Requests': '1'
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

def get_session():
    if HAS_CURL:
        session = crequests.Session(impersonate="chrome120")
    else:
        session = crequests.Session()
    session.headers.update(HEADERS)
    return session

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

    if not keyword or not shop_key:
        return jsonify({"error": "Parameter 'shop' und 'keyword' erforderlich."}), 400

    if shop_key not in SHOP_DOMAINS:
        return jsonify({"error": f"Unbekannter Shop '{shop_key}'."}), 400

    target_domain = SHOP_DOMAINS[shop_key]
    session = get_session()
    products = []

    # Strategie 1: Direktes Amazon Scraping mit Cookie-Warmup
    if shop_key == 'amazon':
        try:
            products = scrape_amazon_direct(session, keyword)
        except Exception as e:
            print(f"Direct Amazon Error: {e}")

    # Strategie 2: DuckDuckGo HTML Fallback
    if len(products) < 5:
        try:
            ddg_prods = search_ddg_html(session, target_domain, keyword, shop_key)
            products.extend(ddg_prods)
            products = deduplicate(products)
        except Exception as e:
            print(f"DDG Error: {e}")

    # Strategie 3: Bing Fallback
    if len(products) < 5:
        try:
            bing_prods = search_bing(session, target_domain, keyword, shop_key)
            products.extend(bing_prods)
            products = deduplicate(products)
        except Exception as e:
            print(f"Bing Error: {e}")

    products = enrich_products(products, shop_key)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products[:30]
    })


def scrape_amazon_direct(session, keyword):
    products = []
   
    # 1. Cookie Handshake (Startseite laden, um Cookies wie session-id zu sammeln)
    try:
        session.get("https://www.amazon.de", timeout=5)
    except Exception:
        pass

    # 2. Suchanfrage senden
    url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}"
    res = session.get(url, timeout=10)
   
    if res.status_code != 200 or "captcha" in res.text.lower():
        print("Amazon direct blocked or CAPTCHA triggered.")
        return products

    soup = BeautifulSoup(res.text, 'html.parser')
    items = soup.find_all('div', {'data-component-type': 's-search-result'})
   
    if not items:
        items = soup.find_all('div', {'data-asin': True})

    for item in items:
        asin = item.get('data-asin', '').strip()
        if not asin or len(asin) != 10:
            continue

        title_tag = item.select_one('h2 a span') or item.select_one('h2 span') or item.select_one('a.a-link-normal span')
        if not title_tag:
            continue

        title = title_tag.get_text().strip()
        if not title:
            continue

        link = f"https://www.amazon.de/dp/{asin}"
        img_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

        price = "-"
        price_tag = item.select_one('.a-price .a-offscreen')
        if price_tag:
            price = price_tag.get_text().strip()
        else:
            p_w = item.select_one('.a-price-whole')
            p_f = item.select_one('.a-price-fraction')
            if p_w:
                w_txt = p_w.get_text().replace('.', '').replace(',', '').strip()
                f_txt = p_f.get_text().strip() if p_f else "00"
                price = f"{w_txt},{f_txt} €"

        products.append({
            "title": title,
            "price": price,
            "imageUrl": img_url,
            "link": link
        })
    return products


def search_ddg_html(session, domain, keyword, shop_key):
    products = []
    query = f"site:{domain} {keyword}" if shop_key != 'amazon' else f"site:amazon.de/dp/ {keyword}"
   
    url = "https://html.duckduckgo.com/html/"
    data = {'q': query}
   
    res = session.post(url, data=data, timeout=10)
    if res.status_code == 200:
        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.select('a.result__url'):
            link = a.get('href', '')
            if 'uddg=' in link:
                match = re.search(r'uddg=([^&]+)', link)
                if match:
                    link = urllib.parse.unquote(match.group(1))

            if not is_valid_url(link, domain):
                continue

            parent = a.find_parent('div', class_='result__body')
            title = ""
            desc = ""
            if parent:
                t_elem = parent.select_one('a.result__snippet') or parent.select_one('h2')
                if t_elem:
                    title = clean_title(t_elem.get_text())
                desc = parent.get_text()

            if not title:
                title = clean_title(a.get_text())

            if not title:
                continue

            price = extract_price(desc)
            products.append({
                "title": title,
                "price": price,
                "imageUrl": "",
                "link": link.split('?')[0]
            })
    return products


def search_bing(session, domain, keyword, shop_key):
    products = []
    query = f"site:{domain} {keyword}" if shop_key != 'amazon' else f"site:amazon.de/dp/ {keyword}"
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
   
    res = session.get(url, timeout=10)
    if res.status_code == 200:
        soup = BeautifulSoup(res.text, 'html.parser')
        for item in soup.select('li.b_algo'):
            a = item.find('a', href=True)
            if not a:
                continue
            link = a['href']
            if not is_valid_url(link, domain):
                continue

            title = clean_title(a.get_text())
            if not title:
                continue

            price = extract_price(item.get_text())
            products.append({
                "title": title,
                "price": price,
                "imageUrl": "",
                "link": link.split('?')[0]
            })
    return products


def is_valid_url(url, domain):
    u = url.lower()
    if domain not in u:
        return False
    bad = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/kontakt', '/b?']
    if any(b in u for b in bad):
        return False
    if 'amazon.de' in domain:
        if not ('/dp/' in u or '/gp/product/' in u or re.search(r'/[a-z0-9]{10}', u)):
            return False
    return True


def clean_title(title):
    if not title:
        return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'(online kaufen|jetzt bestellen|bei Amazon|auf OTTO\.de).*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_price(text):
    if not text:
        return "-"
    match = re.search(r'(\d{1,4}[.,]\d{2})\s*€', text) or re.search(r'€\s*(\d{1,4}[.,]\d{2})', text)
    if match:
        val = match.group(1).replace('.', ',')
        return val if '€' in val else f"{val} €"
    return "-"


def deduplicate(products):
    seen = set()
    unique = []
    for p in products:
        link_clean = p['link'].lower()
        if link_clean not in seen:
            seen.add(link_clean)
            unique.append(p)
    return unique


def enrich_products(products, shop_key):
    for p in products:
        if shop_key == 'amazon' or 'amazon.de' in p['link']:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'], re.IGNORECASE)
            if asin_match:
                asin = asin_match.group(1).upper()
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                if not p['imageUrl']:
                    p['imageUrl'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"
    return products


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
