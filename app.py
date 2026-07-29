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
    session.cookies.set("i18n-prefs", "EUR", domain=".amazon.de")
    session.cookies.set("lc-main", "de_DE", domain=".amazon.de")
    return session

def get_amazon_image_url(asin):
    return f"https://images-eu.ssl-images-amazon.com/images/P/{asin}.03._SCLZZZZZZZ_SX500_.jpg"

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

    if shop_key == 'amazon':
        try:
            products = scrape_amazon_direct(session, keyword)
        except Exception as e:
            print(f"Direct Amazon Error: {e}")

    if len(products) < 25:
        try:
            ddg_prods = search_ddg_html(session, target_domain, keyword, shop_key)
            products.extend(ddg_prods)
            products = deduplicate(products)
        except Exception as e:
            pass

    if len(products) < 25:
        try:
            bing_prods = search_bing(session, target_domain, keyword, shop_key)
            products.extend(bing_prods)
            products = deduplicate(products)
        except Exception as e:
            pass

    products = enrich_products_parallel(products[:30], shop_key)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products
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
        img_tag = item.select_one('img.s-image')
        img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)

        price = "-"
        price_candidates = [
            '.a-price .a-offscreen',
            'span.a-price',
            '.a-color-price',
            '.a-text-price .a-offscreen',
            'span.a-size-base.a-color-price'
        ]
        for selector in price_candidates:
            p_elem = item.select_one(selector)
            if p_elem:
                formatted = format_price_string(p_elem.get_text())
                if formatted != "-":
                    price = formatted
                    break

        if price == "-":
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
    # Flexiblerer Suchansatz (ohne Zwang zum site: Operator)
    queries = [
        f"{domain} {keyword}",
        f"site:{domain} {keyword}"
    ]
    for q in queries:
        url = "https://html.duckduckgo.com/html/"
        res = session.post(url, data={'q': q}, timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.select('a.result__url'):
                link = a.get('href', '')
                if 'uddg=' in link:
                    m = re.search(r'uddg=([^&]+)', link)
                    if m: link = urllib.parse.unquote(m.group(1))
                if not is_valid_url(link, domain): continue
               
                parent = a.find_parent('div', class_='result__body')
                title, desc = "", ""
                if parent:
                    t_elem = parent.select_one('a.result__snippet') or parent.select_one('h2') or parent.select_one('.result__title')
                    if t_elem: title = clean_title(t_elem.get_text())
                    desc = parent.get_text()
               
                if not title: title = clean_title(a.get_text())
                if not title or is_junk_title(title): continue

                products.append({
                    "title": title,
                    "price": extract_price(desc),
                    "imageUrl": "",
                    "link": link.split('?')[0]
                })
        if len(products) >= 30: break
    return products


def search_bing(session, domain, keyword, shop_key):
    products = []
    queries = [
        f"{domain} {keyword}",
        f"site:{domain} {keyword}"
    ]
    for query in queries:
        res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(query)}", timeout=10)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            elements = soup.select('li.b_algo, div.b_algo')
            for item in elements:
                a = item.find('a', href=True)
                if not a: continue
                link = a['href']
                if not is_valid_url(link, domain): continue
                title = clean_title(a.get_text())
                if not title or is_junk_title(title): continue
               
                products.append({
                    "title": title,
                    "price": extract_price(item.get_text()),
                    "imageUrl": "",
                    "link": link.split('?')[0]
                })
        if len(products) >= 30: break
    return products


def fetch_metadata_for_product(product, shop_key):
    if product.get('imageUrl') and product.get('price') != '-':
        return product

    try:
        extra_headers = HEADERS.copy()
        cookies = {"i18n-prefs": "EUR", "lc-main": "de_DE"} if shop_key == 'amazon' or 'amazon.de' in product['link'] else {}

        if HAS_CURL:
            res = crequests.get(product['link'], impersonate="chrome120", headers=extra_headers, cookies=cookies, timeout=8)
        else:
            res = crequests.get(product['link'], headers=extra_headers, cookies=cookies, timeout=8)

        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # 1. Bild extrahieren
            if not product.get('imageUrl'):
                img_url = ""
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'):
                    img_url = og_img['content']
                else:
                    img_tag = soup.find('img', itemprop='image') or soup.find('img', class_=re.compile(r'product.*image|p-image|main-image', re.I))
                    if img_tag and img_tag.get('src'):
                        img_url = img_tag['src']
               
                if img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed = urllib.parse.urlparse(product['link'])
                        img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                    product['imageUrl'] = img_url

            # 2. Preis extrahieren
            if product.get('price') == '-':
                og_price = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount') or soup.find('meta', attrs={'name': 'twitter:data1'})
                if og_price and og_price.get('content'):
                    product['price'] = format_price_string(og_price['content'])

                if product.get('price') == '-':
                    itemprop_price = soup.find(attrs={"itemprop": "price"})
                    if itemprop_price:
                        val = itemprop_price.get("content") or itemprop_price.get_text()
                        product['price'] = format_price_string(val)

                if product.get('price') == '-':
                    for s in soup.find_all('script', type='application/ld+json'):
                        if s.string and '"price"' in s.string:
                            m = re.search(r'"price"\s*:\s*["\']?([\d.,]+)["\']?', s.string)
                            if m:
                                product['price'] = format_price_string(m.group(1))
                                break

                if product.get('price') == '-':
                    price_elem = soup.find(class_=re.compile(r'product-price|current-price|price--current|price-tag|price', re.I))
                    if price_elem:
                        product['price'] = format_price_string(price_elem.get_text())

    except Exception:
        pass
   
    return product


def enrich_products_parallel(products, shop_key):
    for p in products:
        if shop_key == 'amazon' or 'amazon.de' in p['link']:
            asin_match = re.search(r'(?:dp/|gp/product/|/)([A-Z0-9]{10})(?:[\?/]|$)', p['link'], re.IGNORECASE)
            if asin_match:
                asin = asin_match.group(1).upper()
                p['link'] = f"https://www.amazon.de/dp/{asin}"
                if not p.get('imageUrl'):
                    p['imageUrl'] = get_amazon_image_url(asin)

    items_to_fetch = [p for p in products if not p.get('imageUrl') or p.get('price') == '-']
    if items_to_fetch:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(fetch_metadata_for_product, p, shop_key) for p in items_to_fetch]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    pass

    return products


def format_price_string(text):
    if not text: return "-"
    text = text.replace('$', '').replace('USD', '').replace('EUR', '').replace('€', '').strip()
    match = re.search(r'(\d{1,4}[.,]\d{2})', text)
    if match:
        return f"{match.group(1).replace('.', ',')} €"
    match_int = re.search(r'^(\d{1,4})$', text)
    if match_int:
        return f"{match_int.group(1)},00 €"
    return "-"


def is_junk_title(title):
    t = title.strip().lower()
    if len(t) < 8: return True
    for p in JUNK_TITLE_PATTERNS:
        if re.search(p, t): return True
    return False


def is_valid_url(url, domain):
    u = url.lower()
    if domain not in u: return False
    bad = ['/impressum', '/datenschutz', '/agb', '/service', '/filialen', '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/kontakt', '/b?']
    if any(b in u for b in bad): return False
    if 'amazon.de' in domain and not ('/dp/' in u or '/gp/product/' in u or re.search(r'/[a-z0-9]{10}', u)):
        return False
    return True


def clean_title(title):
    if not title: return ""
    clean = re.sub(r'\s*[:|-|•]\s*.*$', '', title)
    clean = re.sub(r'(online kaufen|jetzt bestellen|bei Amazon|auf OTTO\.de).*$', '', clean, flags=re.IGNORECASE)
    return clean.strip()


def extract_price(text):
    if not text: return "-"
    match = re.search(r'(\d{1,4}[.,]\d{2})\s*€', text) or re.search(r'€\s*(\d{1,4}[.,]\d{2})', text)
    if match: return f"{match.group(1).replace('.', ',')} €"
    return "-"


def deduplicate(products):
    seen, unique = set(), []
    for p in products:
        link_clean = p['link'].lower()
        if link_clean not in seen:
            seen.add(link_clean)
            unique.append(p)
    return unique


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
