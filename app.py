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

# -------------------------------------------------------------------
# SHOP-SPEZIFISCHE KONFIGURATION (Keine Pfade im site: Operator!)
# -------------------------------------------------------------------
SHOP_CONFIG = {
    'amazon': {
        'domain': 'amazon.de',
        'queries': lambda kw: [f"site:amazon.de {kw}", f"amazon.de {kw}"],
        'url_check': lambda u: bool(re.search(r'/(dp|gp/product)/[a-z0-9]{10}', u, re.I) or re.search(r'/[a-z0-9]{10}(?:[/?]|$)', u, re.I))
    },
    'norma': {
        'domain': 'norma24.de',
        'queries': lambda kw: [f"site:norma24.de {kw}", f"norma24.de {kw}"],
        'url_check': lambda u: '/kategorie/' not in u and not u.rstrip('/').endswith(('norma24.de', 'norma24.de/de'))
    },
    'netto': {
        'domain': 'netto-online.de',
        'queries': lambda kw: [f"site:netto-online.de {kw}", f"netto-online.de {kw}"],
        'url_check': lambda u: ('/p-' in u or '/p/' in u or '/artikel/' in u or u.endswith('.html')) and '/filialen' not in u and '/prospekt' not in u
    },
    'obi': {
        'domain': 'obi.de',
        'queries': lambda kw: [f"site:obi.de {kw}", f"obi.de {kw}"],
        'url_check': lambda u: '/p/' in u or re.search(r'\d{6,}$', u)
    },
    'hm': {
        'domain': 'hm.com',
        'queries': lambda kw: [f"site:hm.com {kw}", f"hm.com {kw}"],
        'url_check': lambda u: '/productpage' in u
    },
    'ikea': {
        'domain': 'ikea.com',
        'queries': lambda kw: [f"site:ikea.com {kw}", f"ikea.com {kw}"],
        'url_check': lambda u: '/p/' in u
    },
    'jysk': {
        'domain': 'jysk.de',
        'queries': lambda kw: [f"site:jysk.de {kw}", f"jysk.de {kw}"],
        'url_check': lambda u: '/c/' not in u and not u.rstrip('/').endswith('jysk.de')
    },
    'kaufland': {
        'domain': 'kaufland.de',
        'queries': lambda kw: [f"site:kaufland.de {kw}", f"kaufland.de {kw}"],
        'url_check': lambda u: '/product/' in u or '/item/' in u or '/pdp/' in u
    },
    'otto': {
        'domain': 'otto.de',
        'queries': lambda kw: [f"site:otto.de {kw}", f"otto.de {kw}"],
        'url_check': lambda u: '/p/' in u or '#variationid=' in u or '/pdp/' in u
    },
    'smythtoys': {
        'domain': 'smythstoys.com',
        'queries': lambda kw: [f"site:smythstoys.com {kw}", f"smythstoys.com {kw}"],
        'url_check': lambda u: '/p/' in u or '/product/' in u or bool(re.search(r'/de/de/.*?\d{5,}', u))
    },
    'decathlon': {
        'domain': 'decathlon.de',
        'queries': lambda kw: [f"site:decathlon.de {kw}", f"decathlon.de {kw}"],
        'url_check': lambda u: '/p/' in u
    },
    'cna': {
        'domain': 'c-and-a.com',
        'queries': lambda kw: [f"site:c-and-a.com {kw}", f"c-and-a.com {kw}"],
        'url_check': lambda u: '/p/' in u or '/product/' in u
    },
    'bauhaus': {
        'domain': 'bauhaus.info',
        'queries': lambda kw: [f"site:bauhaus.info {kw}", f"bauhaus.info {kw}"],
        'url_check': lambda u: '/p/' in u or '/produkt/' in u
    }
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

    if shop_key not in SHOP_CONFIG:
        return jsonify({"error": f"Unbekannter Shop '{shop_key}'."}), 400

    target_domain = SHOP_CONFIG[shop_key]['domain']
    session = get_session()
    products = []

    # 1. Direkter Amazon-Abruf
    if shop_key == 'amazon':
        try:
            products = scrape_amazon_direct(session, keyword)
        except Exception as e:
            print(f"Direct Amazon Error: {e}")

    # 2. DuckDuckGo Fallback
    if len(products) < 25:
        try:
            ddg_prods = search_ddg_html(session, shop_key, keyword)
            products.extend(ddg_prods)
            products = deduplicate(products)
        except Exception as e:
            pass

    # 3. Bing Fallback
    if len(products) < 25:
        try:
            bing_prods = search_bing(session, shop_key, keyword)
            products.extend(bing_prods)
            products = deduplicate(products)
        except Exception as e:
            pass

    # 4. Meta-Daten im Hintergrund anreichern
    products = enrich_products_parallel(products[:30], shop_key)

    return jsonify({
        "status": "success",
        "shop": shop_key,
        "domain": target_domain,
        "keyword": keyword,
        "count": len(products),
        "products": products
    })


def parse_amazon_item_price(item):
    """Extrem robuster Amazon Preis-Parser direkt auf der Suchkarte"""
    selectors = [
        '.a-price .a-offscreen',
        'span.a-price span.a-offscreen',
        '#corePrice_feature_div .a-offscreen',
        '#corePriceDisplay_desktop_feature_div .a-offscreen',
        '.a-color-price',
        '.a-text-price .a-offscreen',
        'span.a-color-base'
    ]
    for sel in selectors:
        elems = item.select(sel)
        for elem in elems:
            p_str = format_price_string(elem.get_text())
            if p_str != "-":
                return p_str

    p_w = item.select_one('.a-price-whole')
    if p_w:
        p_f = item.select_one('.a-price-fraction')
        w_txt = re.sub(r'[^\d]', '', p_w.get_text())
        f_txt = re.sub(r'[^\d]', '', p_f.get_text()) if p_f else "00"
        if w_txt:
            f_txt_fmt = f_txt if len(f_txt) == 2 else f_txt.ljust(2, '0')
            return f"{w_txt},{f_txt_fmt} €"

    # Regex Fallback direkt im Text der Kachel
    item_text = item.get_text()
    match = re.search(r'(\d{1,4}[.,]\d{2})\s*€', item_text) or re.search(r'€\s*(\d{1,4}[.,]\d{2})', item_text)
    if match:
        return f"{match.group(1).replace('.', ',')} €"

    return "-"


def scrape_amazon_direct(session, keyword):
    products = []
    try:
        session.get("https://www.amazon.de", timeout=4)
    except Exception:
        pass

    url = f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss"
    res = session.get(url, timeout=8)
   
    if res.status_code != 200 or "captcha" in res.text.lower() or "robot check" in res.text.lower():
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
        price = parse_amazon_item_price(item)

        products.append({
            "title": title,
            "price": price,
            "imageUrl": img_url,
            "link": link
        })
    return products


def search_ddg_html(session, shop_key, keyword):
    products = []
    cfg = SHOP_CONFIG[shop_key]
    queries = cfg['queries'](keyword)

    for q in queries:
        try:
            res = session.post("https://html.duckduckgo.com/html/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.select('a.result__url'):
                    link = a.get('href', '')
                    if 'uddg=' in link:
                        m = re.search(r'uddg=([^&]+)', link)
                        if m: link = urllib.parse.unquote(m.group(1))
                   
                    if not is_valid_url(link, shop_key):
                        continue
                   
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
        except Exception:
            pass
        if len(products) >= 25: break
    return products


def search_bing(session, shop_key, keyword):
    products = []
    cfg = SHOP_CONFIG[shop_key]
    queries = cfg['queries'](keyword)

    for query in queries:
        try:
            res = session.get(f"https://www.bing.com/search?q={urllib.parse.quote(query)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                elements = soup.select('li.b_algo, div.b_algo')
                for item in elements:
                    a = item.find('a', href=True)
                    if not a: continue
                    link = a['href']
                   
                    if not is_valid_url(link, shop_key): continue
                   
                    title = clean_title(a.get_text())
                    if not title or is_junk_title(title): continue
                   
                    products.append({
                        "title": title,
                        "price": extract_price(item.get_text()),
                        "imageUrl": "",
                        "link": link.split('?')[0]
                    })
        except Exception:
            pass
        if len(products) >= 25: break
    return products


def fetch_metadata_for_product(product, shop_key):
    if product.get('imageUrl') and product.get('price') != '-':
        return product

    try:
        extra_headers = HEADERS.copy()
        cookies = {"i18n-prefs": "EUR", "lc-main": "de_DE"} if shop_key == 'amazon' or 'amazon.de' in product['link'] else {}

        if HAS_CURL:
            res = crequests.get(product['link'], impersonate="chrome120", headers=extra_headers, cookies=cookies, timeout=6)
        else:
            res = crequests.get(product['link'], headers=extra_headers, cookies=cookies, timeout=6)

        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # 1. BILD EXTRAHIEREN
            if not product.get('imageUrl'):
                img_url = ""
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'):
                    img_url = og_img['content']
                else:
                    img_tag = soup.find('img', itemprop='image') or soup.find('img', class_=re.compile(r'product.*image|p-image|main-image', re.I))
                    if img_tag and img_tag.get('src'):
                        img_url = img_tag['src']
               
                if not img_url:
                    img_match = re.search(r'["\']image["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|jpeg|png|webp))["\']', html, re.I)
                    if img_match:
                        img_url = img_match.group(1)

                if img_url:
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    elif img_url.startswith('/'):
                        parsed = urllib.parse.urlparse(product['link'])
                        img_url = f"{parsed.scheme}://{parsed.netloc}{img_url}"
                    product['imageUrl'] = img_url

            # 2. PREIS EXTRAHIEREN
            if product.get('price') == '-':
                og_price = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_price and og_price.get('content'):
                    product['price'] = format_price_string(og_price['content'])

                if product.get('price') == '-':
                    itemprop_price = soup.find(attrs={"itemprop": "price"})
                    if itemprop_price:
                        val = itemprop_price.get("content") or itemprop_price.get_text()
                        product['price'] = format_price_string(val)

                if product.get('price') == '-':
                    for s in soup.find_all('script'):
                        if s.string:
                            m = re.search(r'["\']price["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', s.string)
                            if m:
                                product['price'] = format_price_string(m.group(1))
                                break

                if product.get('price') == '-':
                    price_elem = soup.find(class_=re.compile(r'product-price|current-price|price--current|price-tag|pdp-price', re.I))
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
    if not title: return True
    t = title.strip().lower()
    if len(t) < 5: return True

    exact_junk = [
        'amazon.de', 'norma24', 'netto online', 'startseite', 'home',
        'willkommen', 'kundenrezensionen', 'produktbeschreibung',
        'impressum', 'datenschutz', 'agb', 'kaufland.de', 'otto.de'
    ]
    if t in exact_junk: return True

    # Gezielter Filter nur für echte SEO-Marketingtext-Blöcke (keine einzelnen Wörter wie 'reduziert'!)
    junk_intros = [
        'info zu diesem artikel', 'wareninformationen', 'präzises design',
        'norma24 online-shop bietet', 'willkommen bei', 'kidland® ist unsere eigenmarke',
        'unsere eigenmarke', 'kaufland bietet ihnen', 'herzlich willkommen'
    ]
    for ji in junk_intros:
        if ji in t:
            return True

    return False


def is_valid_url(url, shop_key):
    if shop_key not in SHOP_CONFIG:
        return False
   
    cfg = SHOP_CONFIG[shop_key]
    domain = cfg['domain']
    u = url.lower()

    if domain not in u:
        return False

    parsed = urllib.parse.urlparse(u)
    path = parsed.path.strip('/')

    if not path or path in ['', 'de', 'de/', 'de_de', 'index.html', 'index.php', 'shop']:
        return False

    bad = [
        '/impressum', '/datenschutz', '/agb', '/service', '/filialen',
        '/warenkorb', '/login', '/hilfe', '/faq', '/jobs', '/kontakt',
        '/suche', '/search', '/cart', '/account', '/myaccount'
    ]
    if any(b in u for b in bad):
        return False

    return cfg['url_check'](u)


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
