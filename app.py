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
# SUCH-HELFER (UNANGETASTET)
# ===================================================================
def execute_ddg_search(session, query, valid_url_func):
    products = []
    try:
        res = session.post("https://html.duckduckgo.com/html/", data={'q': query}, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.select('a.result__url'):
                link = a.get('href', '')
                if 'uddg=' in link:
                    m = re.search(r'uddg=([^&]+)', link)
                    if m: link = urllib.parse.unquote(m.group(1))
               
                link = link.split('?')[0]
                if valid_url_func(link):
                    parent = a.find_parent('div', class_='result__body')
                    title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                    products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
    except Exception:
        pass
    return products


def execute_bing_search(session, query, valid_url_func):
    products = []
    try:
        res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(query)}", timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for item in soup.select('li.b_algo'):
                a = item.find('a', href=True)
                if not a: continue
                link = a['href'].split('?')[0]
                if valid_url_func(link):
                    title = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip()
                    products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
    except Exception:
        pass
    return products


# ===================================================================
# VOLLSTÄNDIGER METADATEN-ENRICHER (UNANGETASTET)
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p

    try:
        cookies = {"i18n-prefs": "EUR", "lc-main": "de_DE"} if shop_key == 'amazon' else {}
        res = session.get(p['link'], cookies=cookies, timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # 1. BILDER
            if not p.get('imageUrl'):
                img_url = ""
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'):
                    img_url = og_img['content']
                else:
                    img_tag = soup.find('img', itemprop='image') or soup.find('img', class_=re.compile(r'product.*image|main-image', re.I))
                    if img_tag and img_tag.get('src'): img_url = img_tag['src']
               
                if not img_url:
                    m = re.search(r'["\']image["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']', html, re.I)
                    if m: img_url = m.group(1)

                if img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed = urllib.parse.urlparse(p['link'])
                        img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                    p['imageUrl'] = img_url

            # 2. PREISE
            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'):
                    p['price'] = format_price_string(og_p['content'])
               
                if p.get('price') == '-':
                    prop = soup.find(attrs={"itemprop": "price"})
                    if prop: p['price'] = format_price_string(prop.get("content") or prop.get_text())
               
                if p.get('price') == '-':
                    for s in soup.find_all('script'):
                        if s.string:
                            m = re.search(r'["\']price["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', s.string)
                            if m:
                                p['price'] = format_price_string(m.group(1))
                                break
    except Exception:
        pass
    return p

def enrich_products_parallel(session, products, shop_key):
    items_to_fetch = [p for p in products if not p.get('imageUrl') or p.get('price') == '-']
    if items_to_fetch:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(enrich_single_product, p, session, shop_key) for p in items_to_fetch]
            for f in futures:
                try: f.result()
                except Exception: pass
    return products

# ===================================================================
# ISOLIERTE SHOP-ROUTINEN
# ===================================================================

# ---> ANGEPASST: Amazon (Pfade aus Suchoperator entfernt)
def scrape_amazon(session, keyword):
    products = []
   
    # 1. Direkter Aufruf
    try:
        session.get("https://www.amazon.de", timeout=4)
        url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss"
        res = session.get(url, timeout=8)
        if res.status_code == 200 and "captcha" not in res.text.lower():
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            items = soup.find_all('div', {'data-component-type': 's-search-result'})
            for item in items:
                asin = item.get('data-asin', '').strip()
                if not asin or len(asin) != 10: continue
                title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                if not title_tag: continue
                title = title_tag.get_text().strip()
                if not title or len(title) < 5: continue

                price = "-"
                p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('.a-color-price')
                if p_elem: price = format_price_string(p_elem.get_text())
                if price == "-":
                    p_w, p_f = item.select_one('.a-price-whole'), item.select_one('.a-price-fraction')
                    if p_w: price = f"{p_w.get_text().replace('.', '').replace(',', '').strip()},{p_f.get_text().strip() if p_f else '00'} €"

                img_tag = item.select_one('img.s-image')
                img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)

                products.append({
                    "title": title, "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"
                })
    except Exception:
        pass

    # 2. Fallbacks (Mit breiter Suche, Python filtert die echten Links via valid_az)
    if len(products) < 20:
        valid_az = lambda u: bool(re.search(r'/(dp|gp/product)/[a-z0-9]{10}', u, re.I) or re.search(r'/[a-z0-9]{10}(?:[/?]|$)', u, re.I))
        products.extend(execute_ddg_search(session, f"amazon.de {keyword}", valid_az))
        if len(products) < 20:
            products.extend(execute_ddg_search(session, f"site:amazon.de {keyword}", valid_az))
        if len(products) < 20:
            products.extend(execute_bing_search(session, f"amazon.de {keyword}", valid_az))
        if len(products) < 20:
            products.extend(execute_bing_search(session, f"site:amazon.de {keyword}", valid_az))

    # ASIN Bilder reparieren, falls beim Fallback das Bild fehlte
    for p in products:
        m = re.search(r'/[a-z0-9]{10}', p['link'], re.I)
        if m and not p.get('imageUrl'):
            asin_cand = m.group(0).strip('/').upper()
            if len(asin_cand) == 10: p['imageUrl'] = get_amazon_image_url(asin_cand)

    return enrich_products_parallel(session, deduplicate(products)[:30], 'amazon')


# ---> UNANGETASTET: Norma (Funktioniert perfekt)
def scrape_norma(session, keyword):
    valid_url = lambda u: 'norma24.de' in u and '/kategorie/' not in u and not u.rstrip('/').endswith(('norma24.de', 'norma24.de/de'))
    products = execute_ddg_search(session, f"site:norma24.de {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:norma24.de {keyword}", valid_url))
   
    # Filtert unerwünschte SEO-Sätze aus
    filtered = [p for p in products if 'norma24 online-shop bietet' not in p['title'].lower()]
    return enrich_products_parallel(session, deduplicate(filtered)[:30], 'norma')


# ---> ANGEPASST: Netto (Breitere Suche + Python-Filter für Produktlinks)
def scrape_netto(session, keyword):
    valid_url = lambda u: 'netto-online.de' in u and ('/p-' in u or '/p/' in u or '/artikel/' in u or u.endswith('.html')) and '/filialen' not in u
    products = execute_ddg_search(session, f"netto-online.de {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_ddg_search(session, f"site:netto-online.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"netto-online.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:netto-online.de {keyword}", valid_url))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'netto')


# ---> ANGEPASST: Kaufland (Breitere Suche + Python-Filter für Produktlinks)
def scrape_kaufland(session, keyword):
    valid_url = lambda u: 'kaufland.de' in u and ('/product/' in u or '/item/' in u or '/pdp/' in u)
    products = execute_ddg_search(session, f"kaufland.de {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_ddg_search(session, f"site:kaufland.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"kaufland.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:kaufland.de {keyword}", valid_url))
   
    # Filtert die Eigenmarken-Texte
    filtered = [p for p in products if 'kidland' not in p['title'].lower() and 'eigenmarke' not in p['title'].lower()]
    return enrich_products_parallel(session, deduplicate(filtered)[:30], 'kaufland')


# ---> ANGEPASST: Otto (Breitere Suche + Python-Filter für Produktlinks)
def scrape_otto(session, keyword):
    valid_url = lambda u: 'otto.de' in u and ('/p/' in u or '#variationid=' in u or '/pdp/' in u)
    products = execute_ddg_search(session, f"otto.de {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_ddg_search(session, f"site:otto.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"otto.de {keyword}", valid_url))
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:otto.de {keyword}", valid_url))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'otto')


def scrape_smythtoys(session, keyword):
    valid_url = lambda u: 'smythstoys.com' in u and ('/p/' in u or '/product/' in u or re.search(r'\d{5,}', u))
    products = execute_ddg_search(session, f"site:smythstoys.com {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:smythstoys.com {keyword}", valid_url))
    return enrich_products_parallel(session, deduplicate(products)[:30], 'smythtoys')


def scrape_generic(session, shop_key, domain, keyword):
    valid_url = lambda u: domain in u and not u.rstrip('/').endswith(domain) and '/impressum' not in u
    products = execute_ddg_search(session, f"site:{domain} {keyword}", valid_url)
    if len(products) < 20:
        products.extend(execute_bing_search(session, f"site:{domain} {keyword}", valid_url))
    return enrich_products_parallel(session, deduplicate(products)[:30], shop_key)


# ===================================================================
# ROUTING (UNANGETASTET)
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
   
    if shop_key == 'amazon':
        products = scrape_amazon(session, keyword)
    elif shop_key == 'norma':
        products = scrape_norma(session, keyword)
    elif shop_key == 'netto':
        products = scrape_netto(session, keyword)
    elif shop_key == 'kaufland':
        products = scrape_kaufland(session, keyword)
    elif shop_key == 'otto':
        products = scrape_otto(session, keyword)
    elif shop_key == 'smythtoys':
        products = scrape_smythtoys(session, keyword)
    else:
        products = scrape_generic(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success", "shop": shop_key, "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword, "count": len(products), "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
