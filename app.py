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

def deduplicate(products):
    seen, unique = set(), []
    for p in products:
        link_clean = p['link'].lower()
        if link_clean not in seen:
            seen.add(link_clean)
            unique.append(p)
    return unique


# ===================================================================
# ISOLIERTER SCRAPER: AMAZON (Eingefrorener Stand)
# ===================================================================
def scrape_amazon(session, keyword):
    products = []
    # 1. Direktabruf
    try:
        session.get("https://www.amazon.de", timeout=4)
        url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss"
        res = session.get(url, timeout=8)
        if res.status_code == 200 and "captcha" not in res.text.lower():
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            items = soup.find_all('div', {'data-component-type': 's-search-result'}) or soup.find_all('div', {'data-asin': True})
            for item in items:
                asin = item.get('data-asin', '').strip()
                if not asin or len(asin) != 10: continue
                title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                if not title_tag: continue
                title = title_tag.get_text().strip()
                if not title or len(title) < 5: continue

                price = "-"
                p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('span.a-price') or item.select_one('.a-color-price')
                if p_elem:
                    price = format_price_string(p_elem.get_text())
                if price == "-":
                    p_w, p_f = item.select_one('.a-price-whole'), item.select_one('.a-price-fraction')
                    if p_w:
                        price = f"{p_w.get_text().replace('.', '').replace(',', '').strip()},{p_f.get_text().strip() if p_f else '00'} €"

                img_tag = item.select_one('img.s-image')
                img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)

                products.append({
                    "title": title,
                    "price": price,
                    "imageUrl": img_url,
                    "link": f"https://www.amazon.de/dp/{asin}"
                })
    except Exception:
        pass

    # 2. Bing Fallback nur für Amazon
    if len(products) < 20:
        try:
            q = f"site:amazon.de/dp/ {keyword}"
            res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(q)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for item in soup.select('li.b_algo'):
                    a = item.find('a', href=True)
                    if not a: continue
                    link = a['href']
                    m = re.search(r'/(dp|gp/product)/([A-Z0-9]{10})', link, re.I)
                    if m:
                        asin = m.group(2).upper()
                        products.append({
                            "title": a.get_text().strip(),
                            "price": "-",
                            "imageUrl": get_amazon_image_url(asin),
                            "link": f"https://www.amazon.de/dp/{asin}"
                        })
        except Exception:
            pass

    return deduplicate(products)[:30]


# ===================================================================
# ISOLIERTER SCRAPER: NORMA (Eingefrorener Stand)
# ===================================================================
def scrape_norma(session, keyword):
    products = []
    queries = [f"site:norma24.de {keyword}", f"norma24.de {keyword}"]
   
    for q in queries:
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
            res = session.get(url, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link:
                        m = re.search(r'uddg=([^&]+)', link)
                        if m: link = urllib.parse.unquote(m.group(1))
                   
                    if 'norma24.de' in link and '/kategorie/' not in link and not link.rstrip('/').endswith(('norma24.de', 'norma24.de/de')):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        title = title.strip()
                        if title and 'norma24 online-shop bietet' not in title.lower():
                            products.append({
                                "title": title,
                                "price": "-",
                                "imageUrl": "",
                                "link": link.split('?')[0]
                            })
        except Exception:
            pass
        if len(products) >= 20: break

    # Meta-Daten anreichern (Bild & Preis)
    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# ISOLIERTER SCRAPER: NETTO
# ===================================================================
def scrape_netto(session, keyword):
    products = []
    queries = [f"site:netto-online.de {keyword}", f"netto-online.de {keyword}"]
   
    for q in queries:
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
            res = session.get(url, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link:
                        m = re.search(r'uddg=([^&]+)', link)
                        if m: link = urllib.parse.unquote(m.group(1))
                   
                    if 'netto-online.de' in link and ('/p-' in link or '/p/' in link or '/artikel/' in link):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        products.append({
                            "title": title.strip(),
                            "price": "-",
                            "imageUrl": "",
                            "link": link.split('?')[0]
                        })
        except Exception:
            pass
        if len(products) >= 20: break

    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# ISOLIERTER SCRAPER: KAUFLAND
# ===================================================================
def scrape_kaufland(session, keyword):
    products = []
    queries = [f"site:kaufland.de/product {keyword}", f"site:kaufland.de {keyword}"]
   
    for q in queries:
        try:
            url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
            res = session.get(url, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link:
                        m = re.search(r'uddg=([^&]+)', link)
                        if m: link = urllib.parse.unquote(m.group(1))
                   
                    if 'kaufland.de' in link and ('/product/' in link or '/item/' in link):
                        parent = a.find_parent('div', class_='result__body')
                        title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                        t_clean = title.strip()
                        if 'kidland' not in t_clean.lower() and 'eigenmarke' not in t_clean.lower():
                            products.append({
                                "title": t_clean,
                                "price": "-",
                                "imageUrl": "",
                                "link": link.split('?')[0]
                            })
        except Exception:
            pass
        if len(products) >= 20: break

    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# ISOLIERTER SCRAPER: OTTO
# ===================================================================
def scrape_otto(session, keyword):
    products = []
    q = f"site:otto.de/p {keyword}"
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.select('a.result__url'):
                link = a.get('href', '')
                if 'uddg=' in link:
                    m = re.search(r'uddg=([^&]+)', link)
                    if m: link = urllib.parse.unquote(m.group(1))
               
                if 'otto.de' in link and ('/p/' in link or '#variationid=' in link):
                    parent = a.find_parent('div', class_='result__body')
                    title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                    products.append({
                        "title": title.strip(),
                        "price": "-",
                        "imageUrl": "",
                        "link": link.split('?')[0]
                    })
    except Exception:
        pass

    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# ISOLIERTER SCRAPER: SMYTH TOYS
# ===================================================================
def scrape_smythtoys(session, keyword):
    products = []
    q = f"site:smythstoys.com {keyword}"
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.select('a.result__url'):
                link = a.get('href', '')
                if 'uddg=' in link:
                    m = re.search(r'uddg=([^&]+)', link)
                    if m: link = urllib.parse.unquote(m.group(1))
               
                if 'smythstoys.com' in link and ('/p/' in link or '/product/' in link or re.search(r'\d{5,}', link)):
                    parent = a.find_parent('div', class_='result__body')
                    title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                    products.append({
                        "title": title.strip(),
                        "price": "-",
                        "imageUrl": "",
                        "link": link.split('?')[0]
                    })
    except Exception:
        pass

    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# GENERIC SCRAPER HANDLER (Für übrige Shops)
# ===================================================================
def scrape_generic_shop(session, shop_key, domain, keyword):
    products = []
    q = f"site:{domain} {keyword}"
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(q)}"
        res = session.get(url, timeout=8)
        if res.status_code == 200:
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for a in soup.select('a.result__url'):
                link = a.get('href', '')
                if 'uddg=' in link:
                    m = re.search(r'uddg=([^&]+)', link)
                    if m: link = urllib.parse.unquote(m.group(1))
               
                if domain in link and not link.rstrip('/').endswith(domain):
                    parent = a.find_parent('div', class_='result__body')
                    title = parent.select_one('a.result__snippet').get_text() if parent and parent.select_one('a.result__snippet') else a.get_text()
                    products.append({
                        "title": title.strip(),
                        "price": "-",
                        "imageUrl": "",
                        "link": link.split('?')[0]
                    })
    except Exception:
        pass

    return enrich_generic_products(session, deduplicate(products)[:30])


# ===================================================================
# HELPER: META-DATEN ENRICHER FOR NON-AMAZON SHOPS
# ===================================================================
def enrich_single_product(p, session):
    try:
        res = session.get(p['link'], timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # Bild
            if not p.get('imageUrl'):
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'):
                    p['imageUrl'] = og_img['content']
                else:
                    m_img = re.search(r'["\']image["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']', html, re.I)
                    if m_img: p['imageUrl'] = m_img.group(1)

            # Preis
            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'):
                    p['price'] = format_price_string(og_p['content'])
                else:
                    m_p = re.search(r'["\']price["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', html)
                    if m_p: p['price'] = format_price_string(m_p.group(1))
    except Exception:
        pass
    return p

def enrich_generic_products(session, products):
    items = [p for p in products if not p.get('imageUrl') or p.get('price') == '-']
    if items:
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(enrich_single_product, p, session) for p in items]
            for f in futures:
                try: f.result()
                except Exception: pass
    return products


# ===================================================================
# SHOP-REGISTRIERUNG & ROUTING
# ===================================================================
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

    session = get_session()
    products = []

    # STRIKTE TRENNUNG: Jeder Shop ruft NUR seinen eigenen Scraper auf!
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
        # Generischer Scraper für den Rest
        products = scrape_generic_shop(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword,
        "count": len(products),
        "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
