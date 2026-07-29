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

JUNK_TITLE_PATTERNS = [
    r'^info zu diesem artikel',
    r'^wareninformationen',
    r'^amazon\.de',
    r'^präzises design',
    r'^produktbeschreibung',
    r'^kundenrezensionen',
    r'^details'
]

def get_session():
    if HAS_CURL:
        session = crequests.Session(impersonate="chrome120")
    else:
        session = crequests.Session()
    session.headers.update(HEADERS)
    # Zwingt Amazon auf EUR und Deutsch (hebelt US-IP Währungsumrechnung aus)
    session.cookies.set("i18n-prefs", "EUR", domain=".amazon.de")
    session.cookies.set("lc-main", "de_DE", domain=".amazon.de")
    return session

def get_amazon_image_url(asin):
    return f"https://ws-eu.amazon-adsystem.com/widgets/q?_encoding=UTF-8&ASIN={asin}&Format=_SL300_&ID=AsinImage&MarketPlace=DE&ServiceVersion=20070822&WS=1"

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

    # Strategie 1: Direktes Amazon Scraping
    if shop_key == 'amazon':
        try:
            products = scrape_amazon_direct(session, keyword)
        except Exception as e:
            print(f"Direct Amazon Error: {e}")

    # Strategie 2: DuckDuckGo Fallback
    if len(products) < 25:
        try:
            ddg_prods = search_ddg_html(session, target_domain, keyword, shop_key)
            products.extend(ddg_prods)
            products = deduplicate(products)
        except Exception as e:
            print(f"DDG Error: {e}")

    # Strategie 3: Bing Fallback
    if len(products) < 25:
        try:
            bing_prods = search_bing(session, target_domain, keyword, shop_key)
            products.extend(bing_prods)
            products = deduplicate(products)
        except Exception as e:
            print(f"Bing Error: {e}")

    # Universelles Anreichern von Bildern & Euro-Preisen für ALLE Shops (Norma, OBI, OTTO etc.)
    products = enrich_products_parallel(session, products[:30], shop_key)

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
   
    try:
        session.get("https://www.amazon.de", timeout=5)
    except Exception:
        pass

    url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss"
    res = session.get(url, timeout=10)
   
    if res.status_code != 200 or "captcha" in res.text.lower():
        print("Amazon direct blocked or CAPTCHA triggered.")
        return products

    html_content = res.content.decode('utf-8', 'ignore')
    soup = BeautifulSoup(html_content, 'html.parser')
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
        if not title or is_junk_title(title):
            continue

        link = f"https://www.amazon.de/dp/{asin}"
        img_url = get_amazon_image_url(asin)

        price = "-"
        price_tag = item.select_one('.a-price .a-offscreen')
        if price_tag:
            price = format_price_string(price_tag.get_text())
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
    queries = [
        f"site:{domain}/dp/ {keyword}" if shop_key == 'amazon' else f"site:{domain} {keyword}",
        f"site:{domain} {keyword} kaufen" if shop_key == 'amazon' else f"site:{domain} {keyword} produkt"
    ]

    for q in queries:
        url = "https://html.duckduckgo.com/html/"
        data = {'q': q}
       
        res = session.post(url, data=data, timeout=10)
        if res.status_code == 200:
            html_text = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html_text, 'html.parser')
           
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
                    t_elem = parent.select_one('a.result__snippet') or parent.select_one('h2') or parent.select_one('.result__title')
                    if t_elem:
                        title = clean_title(t_elem.get_text())
                    desc = parent.get_text()

                if not title:
                    title = clean_title(a.get_text())

                if not title or is_junk_title(title):
                    continue

                price = extract_price(desc)
                products.append({
                    "title": title,
                    "price": price,
                    "imageUrl": "",
                    "link": link.split('?')[0]
                })
        if len(products) >= 30:
            break

    return products


def search_bing(session, domain, keyword, shop_key):
    products = []
    query = f"site:{domain}/dp/ {keyword}" if shop_key == 'amazon' else f"site:{domain} {keyword}"
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}"
   
    res = session.get(url, timeout=10)
    if res.status_code == 200:
        html_text = res.content.decode('utf-8', 'ignore')
        soup = BeautifulSoup(html_text, 'html.parser')
        for item in soup.select('li.b_algo'):
            a = item.find('a', href=True)
            if not a:
                continue
            link = a['href']
            if not is_valid_url(link, domain):
                continue

            title = clean_title(a.get_text())
            if not title or is_junk_title(title):
                continue

            price = extract_price(item.get_text())
            products.append({
                "title": title,
                "price": price,
                "imageUrl": "",
                "link": link.split('?')[0]
            })
    return products


def fetch_metadata_for_product(session, product):
    # Liest Bild und Preis direkt aus den OpenGraph/Schema.org Meta-Tags der Produktseite aus
    if product.get('imageUrl') and product.get('price') != '-':
        return product

    try:
        res = session.get(product['link'], timeout=4)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # 1. Bild extrahieren (og:image / twitter:image)
            if not product.get('imageUrl'):
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'):
                    img_url = og_img['content']
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed_link = urllib.parse.urlparse(product['link'])
                        img_url = f"{parsed_link.scheme}://{parsed_link.netloc}{img_url}"
                    product['imageUrl'] = img_url

            # 2. Preis extrahieren (product:price:amount / JSON-LD)
            if product.get('price') == '-':
                og_price = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_price and og_price.get('content'):
                    product['price'] = format_price_string(og_price['content'])
                else:
                    scripts = soup.find_all('script', type='application/ld+json')
                    for s in scripts:
                        if s.string and '"price"' in s.string:
                            m = re.search(r'"price"\s*:\s*["\']?([\d.,]+)["\']?', s.string)
                            if m:
                                product['price'] = format_price_string(m.group(1))
                                break
    except Exception:
        pass
    return product


def enrich_products_parallel(session, products, shop_key):
    # Amazon-spezifisches Bild erzwingen
    for p in products:
        if shop_key == 'amazon' or 'amazon.de' in p['link']:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'], re.IGNORECASE)
            if asin_match:
                asin = asin_match.group(1).upper()
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                p['imageUrl'] = get_amazon_image_url(asin)

    # Paralleler Abruf der Meta-Daten für fehlende Bilder & Preise (Norma, Otto, Netto etc.)
    items_to_fetch = [p for p in products if not p.get('imageUrl') or p.get('price') == '-']
    if items_to_fetch:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(fetch_metadata_for_product, session, p) for p in items_to_fetch]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass

    return products


def format_price_string(text):
    if not text:
        return "-"
    # Wandelt US-Dollar Zeichen oder Punkte sauber in Euro-Format um
    text = text.replace('$', '').replace('USD', '').replace('EUR', '').strip()
    match = re.search(r'(\d{1,4}[.,]\d{2})', text)
    if match:
        val = match.group(1).replace('.', ',')
        return f"{val} €"
    return "-"


def is_junk_title(title):
    t = title.strip().lower()
    if len(t) < 8:
        return True
    for pattern in JUNK_TITLE_PATTERNS:
        if re.search(pattern, t):
            return True
    return False


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
        return f"{val} €"
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


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
