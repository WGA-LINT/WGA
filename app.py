from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
import re
import urllib.parse

# curl_cffi imitiert echte Browser-TLS-Signaturen
try:
    from curl_cffi import requests as crequests
    USE_CURL_CFFI = True
except ImportError:
    import requests as crequests
    USE_CURL_CFFI = False

app = Flask(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
    'Accept-Language': 'de-DE,de;q=0.9,en-US;q=0.8',
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

def make_request(url, method="GET", data=None):
    """Führt HTTP-Requests mit Chrome-TLS-Impersonation aus"""
    try:
        if USE_CURL_CFFI:
            if method == "POST":
                return crequests.post(url, data=data, headers=HEADERS, impersonate="chrome120", timeout=12)
            return crequests.get(url, headers=HEADERS, impersonate="chrome120", timeout=12)
        else:
            if method == "POST":
                return crequests.post(url, data=data, headers=HEADERS, timeout=12)
            return crequests.get(url, headers=HEADERS, timeout=12)
    except Exception as e:
        print(f"Request Error [{url}]: {e}")
        return None

@app.route('/')
def home():
    return jsonify({
        "status": "active",
        "engine": "curl_cffi TLS Impersonator",
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
   
    try:
        products = []
       
        # 1. DuckDuckGo HTML Index
        products = search_duckduckgo(target_domain, keyword)
       
        # 2. Yahoo Search Fallback
        if len(products) < 5:
            yahoo_prods = search_yahoo(target_domain, keyword)
            products.extend(yahoo_prods)
            products = remove_duplicates(products)
           
        # 3. Bing Search Fallback
        if len(products) < 5:
            bing_prods = search_bing(target_domain, keyword)
            products.extend(bing_prods)
            products = remove_duplicates(products)

        # Nachbearbeitung von Amazon-Bildern
        if shop_key == 'amazon':
            for p in products:
                asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'])
                if asin_match:
                    asin = asin_match.group(1)
                    p['link'] = f"https://www.amazon.de/dp/{asin}"
                    p['imageUrl'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_SX300_.jpg"

        return jsonify({
            "status": "success",
            "shop": shop_key,
            "domain": target_domain,
            "keyword": keyword,
            "count": len(products),
            "products": products
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def search_duckduckgo(domain, keyword):
    products = []
    seen = set()
    url = "https://html.duckduckgo.com/html/"
    data = {'q': f'site:{domain} {keyword}'}
   
    res = make_request(url, method="POST", data=data)
    if res and res.status_code == 200:
        soup = BeautifulSoup(res.text, 'html.parser')
        for a in soup.find_all('a', class_='result__a'):
            raw_href = a.get('href', '')
            link = urllib.parse.unquote(raw_href.split('uddg=')[1].split('&')[0]) if 'uddg=' in raw_href else raw_href
           
            if not is_valid_product_url(link, domain):
                continue
               
            clean_link = link.split('?')[0]
            if clean_link in seen:
                continue
               
            title = clean_product_title(a.get_text(), domain)
            if not title or len(title) < 3:
                continue
               
            seen.add(clean_link)
           
            price = "-"
            parent = a.find_parent('div', class_='result__body')
            if parent:
                snippet = parent.find('a', class_='result__snippet')
                if snippet:
                    price = extract_price_from_text(snippet.get_text())
                   
            img_url = extract_image_from_soup(parent)

            products.append({
                "title": title,
                "price": price,
                "imageUrl": img_url,
                "link": clean_link
            })
            if len(products) >= 30:
                break
    return products


def search_yahoo(domain, keyword):
    products = []
    seen = set()
    url = f"https://search.yahoo.com/search?p=site:{domain}+{urllib.parse.quote(keyword)}"
   
    res = make_request(url)
    if res and res.status_code == 200:
        soup = BeautifulSoup(res.text, 'html.parser')
        for div in soup.find_all('div', class_='compTitle'):
            a = div.find('a', href=True)
            if not a:
                continue
            link = a['href']
            if not is_valid_product_url(link, domain):
                continue
               
            clean_link = link.split('?')[0]
            if clean_link in seen:
                continue
               
            title = clean_product_title(a.get_text(), domain)
            if not title or len(title) < 3:
                continue
               
            seen.add(clean_link)
            parent = div.find_parent('div', class_='dd')
            price = extract_price_from_text(parent.get_text()) if parent else "-"
            img_url = extract_image_from_soup(parent)

            products.append({
                "title": title,
                "price": price,
                "imageUrl": img_url,
                "link": clean_link
            })
            if len(products) >= 30:
                break
    return products


def search_bing(domain, keyword):
    products = []
    seen = set()
    url = f"https://www.bing.com/search?q=site:{domain}+{urllib.parse.quote(keyword)}"
   
    res = make_request(url)
    if res and res.status_code == 200:
        soup = BeautifulSoup(res.text, 'html.parser')
        for item in soup.find_all('li', class_='b_algo'):
            a = item.find('a', href=True)
            if not a:
                continue
            link = a['href']
            if not is_valid_product_url(link, domain):
                continue
               
            clean_link = link.split('?')[0]
            if clean_link in seen:
                continue
               
            title = clean_product_title(a.get_text(), domain)
            if not title or len(title) < 3:
                continue
               
            seen.add(clean_link)
            price = extract_price_from_text(item.get_text())
            img_url = extract_image_from_soup(item)

            products.append({
                "title": title,
                "price": price,
                "imageUrl": img_url,
                "link": clean_link
            })
            if len(products) >= 30:
                break
    return products


def is_valid_product_url(url, domain):
    u = url.lower()
    if domain not in u:
        return False
   
    bad = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/presse', '/kontakt']
    for b in bad:
        if b in u:
            return False
           
    if 'amazon.de' in domain and not ('/dp/' in u or '/gp/product/' in u or '/asin/' in u):
        return False
       
    return True


def clean_product_title(title, domain):
    if not title:
        return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'online kaufen.*$', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'auf OTTO\.de.*$', '', clean, flags=re.IGNORECASE)
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


def extract_image_from_soup(soup_item):
    if soup_item:
        img_tag = soup_item.find('img')
        if img_tag and img_tag.get('src') and 'http' in img_tag['src']:
            return img_tag['src']
    return ""


def remove_duplicates(products):
    seen = set()
    unique = []
    for p in products:
        if p['title'] not in seen:
            seen.add(p['title'])
            unique.append(p)
    return unique
