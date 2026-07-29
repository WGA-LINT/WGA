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
# GEMEINSAMER METADATEN-ENRICHER (Unangetastet - repariert Bilder!)
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p

    try:
        cookies = {"i18n-prefs": "EUR", "lc-main": "de_DE"} if shop_key == 'amazon' else {}
        res = session.get(p['link'], cookies=cookies, timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # Bilder abgreifen & ggf. relative Links reparieren
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
                    if img_url.startswith('//'): img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed = urllib.parse.urlparse(p['link'])
                        img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                    p['imageUrl'] = img_url

            # Preise auslesen
            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'): p['price'] = format_price_string(og_p['content'])
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
# 100% ISOLIERTE SHOP-ROUTINEN (Keine geteilten Suchhelfer mehr!)
# ===================================================================

def scrape_amazon(session, keyword):
    products = []
    # 1. Direkter Aufruf
    try:
        session.get("https://www.amazon.de", timeout=4)
        url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss"
        res = session.get(url, timeout=8)
        if res.status_code == 200 and "captcha" not in res.text.lower():
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for item in soup.find_all('div', {'data-component-type': 's-search-result'}):
                asin = item.get('data-asin', '').strip()
                if not asin or len(asin) != 10: continue
                title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                if not title_tag: continue
                title = title_tag.get_text().strip()
                price = "-"
                p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('.a-color-price')
                if p_elem: price = format_price_string(p_elem.get_text())
                img_tag = item.select_one('img.s-image')
                img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)
                products.append({"title": title, "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"})
    except Exception:
        pass

    # 2. Eigener DuckDuckGo Fallback (GET)
    if len(products) < 20:
        queries = [f"amazon.de {keyword}", f"site:amazon.de {keyword}"]
        for q in queries:
            try:
                res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.select('a.result__url'):
                        link = a.get('href', '')
                        if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        link = link.split('?')[0]
                        if re.search(r'/(dp|gp/product)/[a-z0-9]{10}', link, re.I) or re.search(r'/[a-z0-9]{10}(?:[/?]|$)', link, re.I):
                            parent = a.find_parent('div', class_='result__body')
                            title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break

    # 3. Eigener Bing Fallback (GET)
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if re.search(r'/(dp|gp/product)/[a-z0-9]{10}', link, re.I) or re.search(r'/[a-z0-9]{10}(?:[/?]|$)', link, re.I):
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break

    for p in products:
        m = re.search(r'/[a-z0-9]{10}', p['link'], re.I)
        if m and not p.get('imageUrl'):
            asin_cand = m.group(0).strip('/').upper()
            if len(asin_cand) == 10: p['imageUrl'] = get_amazon_image_url(asin_cand)

    return enrich_products_parallel(session, deduplicate(products)[:30], 'amazon')


def scrape_norma(session, keyword):
    products = []
    queries = [f"site:norma24.de {keyword}"]
   
    # Eigener DuckDuckGo Suche (GET)
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if 'norma24.de' in link and '/kategorie/' not in link and not link.rstrip('/').endswith(('norma24.de', 'norma24.de/de')):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                        if title and 'norma24 online-shop bietet' not in title.lower():
                            products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass

    # Eigener Bing Fallback (GET)
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if 'norma24.de' in link and '/kategorie/' not in link and not link.rstrip('/').endswith(('norma24.de', 'norma24.de/de')):
                            title = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip()
                            if title and 'norma24 online-shop bietet' not in title.lower():
                                products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass

    return enrich_products_parallel(session, deduplicate(products)[:30], 'norma')


def scrape_netto(session, keyword):
    products = []
    queries = [f"netto-online.de {keyword}", f"site:netto-online.de {keyword}"]
   
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if 'netto-online.de' in link and ('/p-' in link or '/p/' in link or '/artikel/' in link or link.endswith('.html')) and '/filialen' not in link:
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip(), "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 20: break
       
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if 'netto-online.de' in link and ('/p-' in link or '/p/' in link or '/artikel/' in link or link.endswith('.html')) and '/filialen' not in link:
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'netto')


def scrape_kaufland(session, keyword):
    products = []
    queries = [f"kaufland.de {keyword}", f"site:kaufland.de {keyword}"]
   
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if 'kaufland.de' in link and ('/product/' in link or '/item/' in link or '/pdp/' in link):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        title = re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip()
                        if 'kidland' not in title.lower() and 'eigenmarke' not in title.lower():
                            products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 20: break
       
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if 'kaufland.de' in link and ('/product/' in link or '/item/' in link or '/pdp/' in link):
                            title = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip()
                            if 'kidland' not in title.lower() and 'eigenmarke' not in title.lower():
                                products.append({"title": title, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break
           
    return enrich_products_parallel(session, deduplicate(products)[:30], 'kaufland')


def scrape_otto(session, keyword):
    products = []
    queries = [f"otto.de {keyword}", f"site:otto.de {keyword}"]
   
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if 'otto.de' in link and ('/p/' in link or '#variationid=' in link or '/pdp/' in link):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip(), "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 20: break
       
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if 'otto.de' in link and ('/p/' in link or '#variationid=' in link or '/pdp/' in link):
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break
           
    return enrich_products_parallel(session, deduplicate(products)[:30], 'otto')


def scrape_smythtoys(session, keyword):
    products = []
    queries = [f"site:smythstoys.com {keyword}"]
   
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if 'smythstoys.com' in link and ('/p/' in link or '/product/' in link or re.search(r'\d{5,}', link)):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip(), "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 20: break
       
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if 'smythstoys.com' in link and ('/p/' in link or '/product/' in link or re.search(r'\d{5,}', link)):
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break
           
    return enrich_products_parallel(session, deduplicate(products)[:30], 'smythtoys')


def scrape_generic(session, shop_key, domain, keyword):
    products = []
    queries = [f"site:{domain} {keyword}"]
   
    for q in queries:
        try:
            res = session.get(f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                    link = link.split('?')[0]
                    if domain in link and not link.rstrip('/').endswith(domain) and '/impressum' not in link:
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', title).strip(), "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 20: break
       
    if len(products) < 20:
        for q in queries:
            try:
                res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for item in soup.select('li.b_algo'):
                        a = item.find('a', href=True)
                        if not a: continue
                        link = a['href'].split('?')[0]
                        if domain in link and not link.rstrip('/').endswith(domain) and '/impressum' not in link:
                            products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text()).strip(), "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 20: break
           
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
