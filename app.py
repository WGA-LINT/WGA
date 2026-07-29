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
# METADATEN-ENRICHER (Unangetastet - Liest JSON-Daten für Otto & Co)
# ===================================================================
def enrich_single_product(p, session, shop_key):
    if p.get('imageUrl') and p.get('price') != '-': return p

    try:
        cookies = {"i18n-prefs": "EUR", "lc-main": "de_DE"} if shop_key == 'amazon' else {}
        res = session.get(p['link'], cookies=cookies, timeout=6)
        if res.status_code == 200:
            html = res.content.decode('utf-8', 'ignore')
            soup = BeautifulSoup(html, 'html.parser')

            # JSON-LD Scanner
            for s in soup.find_all('script', type='application/ld+json'):
                txt = s.string
                if txt:
                    if not p.get('imageUrl'):
                        m_img = re.search(r'"image"\s*:\s*(?:\[\s*)?["\'](https?://[^"\']+)["\']', txt)
                        if m_img: p['imageUrl'] = m_img.group(1)
                    if p.get('price') == '-':
                        m_p = re.search(r'"price"\s*:\s*["\']?(\d+[\.,]\d{2}|\d+)["\']?', txt)
                        if m_p: p['price'] = format_price_string(m_p.group(1))

            # Bilder Fallback
            if not p.get('imageUrl'):
                img_url = ""
                og_img = soup.find('meta', property='og:image') or soup.find('meta', attrs={'name': 'twitter:image'})
                if og_img and og_img.get('content'): img_url = og_img['content']
                else:
                    img_tag = soup.find('img', itemprop='image') or soup.find('img', class_=re.compile(r'product.*image|main-image|pdp', re.I))
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

            # Preise Fallback
            if p.get('price') == '-':
                og_p = soup.find('meta', property='product:price:amount') or soup.find('meta', property='og:price:amount')
                if og_p and og_p.get('content'): p['price'] = format_price_string(og_p['content'])
                if p.get('price') == '-':
                    prop = soup.find(attrs={"itemprop": "price"})
                    if prop: p['price'] = format_price_string(prop.get("content") or prop.get_text())
                if p.get('price') == '-':
                    m_p = re.search(r'["\'](?:price|amount)["\']\s*:\s*["\']?(\d+[\.,]\d{2})["\']?', html, re.I)
                    if m_p: p['price'] = format_price_string(m_p.group(1))
                   
            if shop_key == 'otto':
                if p.get('price') == '-':
                    m_otto_p = re.search(r'data-qa=["\']price["\'][^>]*>([^<]+)', html)
                    if m_otto_p: p['price'] = format_price_string(m_otto_p.group(1))
                if not p.get('imageUrl'):
                    m_otto_img = re.search(r'["\'](https://[^"\']+(?:otto\.de|obg-media\.com)[^"\']+\.(?:jpg|webp))["\']', html)
                    if m_otto_img: p['imageUrl'] = m_otto_img.group(1)
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

# 1. AMAZON (Zu 100% eingefroren, da es perfekt funktioniert)
def scrape_amazon(session, keyword):
    products = []
    try:
        session.get("https://www.amazon.de", timeout=4)
        res = session.get(f"https://www.amazon.de/s?k={urllib.parse.quote(keyword)}&ref=nb_sb_noss", timeout=8)
        if res.status_code == 200 and "captcha" not in res.text.lower():
            soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
            for item in soup.find_all('div', {'data-component-type': 's-search-result'}):
                asin = item.get('data-asin', '').strip()
                if not asin or len(asin) != 10: continue
                title_tag = item.select_one('h2 a span') or item.select_one('h2 span')
                if not title_tag: continue
               
                item_str = str(item)
                price = "-"
               
                p_elem = item.select_one('.a-price .a-offscreen') or item.select_one('span.a-price') or item.select_one('.a-color-price')
                if p_elem: price = format_price_string(p_elem.get_text())
               
                if price == "-":
                    p_w = item.select_one('.a-price-whole')
                    p_f = item.select_one('.a-price-fraction')
                    if p_w:
                        w_text = re.sub(r'[^\d]', '', p_w.get_text())
                        f_text = re.sub(r'[^\d]', '', p_f.get_text()) if p_f else '00'
                        if w_text: price = f"{w_text},{f_text} €"
               
                if price == "-":
                    m_pr = re.search(r'(\d{1,4}[.,]\d{2})\s*€', item_str)
                    if m_pr: price = f"{m_pr.group(1).replace('.', ',')} €"

                img_tag = item.select_one('img.s-image')
                img_url = img_tag['src'] if img_tag and img_tag.has_attr('src') else get_amazon_image_url(asin)
                products.append({"title": title_tag.get_text().strip(), "price": price, "imageUrl": img_url, "link": f"https://www.amazon.de/dp/{asin}"})
    except Exception: pass

    if len(products) < 20:
        valid_az = lambda u: bool(re.search(r'/(dp|gp/product)/[a-z0-9]{10}', u, re.I) or re.search(r'/[a-z0-9]{10}(?:[/?]|$)', u, re.I))
        try:
            res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote('site:amazon.de/dp/ ' + keyword)}", timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for div in soup.find_all('div', class_='algo'):
                    a = div.find('a', href=True)
                    if not a: continue
                    link = a['href']
                    if 'RU=' in link: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                    link = link.split('?')[0]
                    if valid_az(link):
                        products.append({"title": re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip(), "price": "-", "imageUrl": "", "link": link})
        except Exception: pass

    for p in products:
        m = re.search(r'/[a-z0-9]{10}', p['link'], re.I)
        if m and not p.get('imageUrl'):
            asin_cand = m.group(0).strip('/').upper()
            if len(asin_cand) == 10: p['imageUrl'] = get_amazon_image_url(asin_cand)

    return enrich_products_parallel(session, deduplicate(products)[:30], 'amazon')


# 2. NORMA (Mit hartem Anti-Quatsch Filter + POST Bypass)
def scrape_norma(session, keyword):
    products = []
   
    def valid_norma_link(url, title):
        u_low, t_low = url.lower(), title.lower()
        if 'norma24.de' not in u_low: return False
       
        # Strenge URL-Ausschlussliste (Header & Kategorien)
        url_blocks = ['/datenschutz', '/impressum', '/agb', '/kontakt', '/newsletter', '/filialen', '/konto', '/warenkorb', '/kategorie', '/aktionen', '/suche', '/anmelden', '/login']
        if any(b in u_low for b in url_blocks): return False
       
        # Strenge Titel-Ausschlussliste
        title_blocks = ['datenschutz', 'impressum', 'agb', 'möbel', 'einrichtung', 'haushalt', 'küche', 'camping', 'freizeit', 'baumarkt', 'anmelden', 'übersicht', 'bestellungen', 'mein profil', 'registrieren', 'e-mobilität', 'elektronik', 'norma24 online-shop', 'startseite']
        if any(b in t_low for b in title_blocks): return False
       
        if u_low.rstrip('/').endswith(('norma24.de', 'norma24.de/de')): return False
        if len(t_low) < 8 or t_low.startswith('www'): return False
        return True

    queries = [f"site:norma24.de {keyword}"]
   
    # DDG Lite POST Bypass (Umgeht Render Block)
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_norma_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    # Yahoo Fallback
    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_norma_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'norma')


# 3. NETTO (POST Bypass)
def scrape_netto(session, keyword):
    products = []
   
    def valid_netto_link(link, title):
        if 'netto-online.de' not in link: return False
        if not any(x in link for x in ['/p-', '/p/', '/artikel/', '.html']): return False
        if '/filialen' in link or '/angebote' in link: return False
        if len(title) < 5 or title.lower().startswith('www'): return False
        return True

    queries = [f"site:netto-online.de {keyword}", f"netto-online.de {keyword}"]
   
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_netto_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_netto_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'netto')


# 4. KAUFLAND (POST Bypass)
def scrape_kaufland(session, keyword):
    products = []
   
    def valid_kaufland_link(link, title):
        if 'kaufland.de' not in link: return False
        if not any(x in link for x in ['/product/', '/item/', '/pdp/']): return False
        t_low = title.lower()
        if len(t_low) < 5 or t_low.startswith('www') or 'kaufland' in t_low or 'kidland' in t_low: return False
        return True

    queries = [f"site:kaufland.de {keyword}", f"kaufland.de {keyword}"]
   
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_kaufland_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_kaufland_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'kaufland')


# 5. OTTO (POST Bypass)
def scrape_otto(session, keyword):
    products = []
   
    def valid_otto_link(link, title):
        if 'otto.de' not in link: return False
        if not any(x in link for x in ['/p/', '#variationid=', '/pdp/']): return False
        if len(title) < 5 or title.lower().startswith('www'): return False
        return True

    queries = [f"site:otto.de {keyword}", f"otto.de {keyword}"]
   
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_otto_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_otto_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'otto')


# 6. SMYTH TOYS (POST Bypass)
def scrape_smythtoys(session, keyword):
    products = []
   
    def valid_smyth_link(link, title):
        if 'smythstoys.com' not in link: return False
        if not any(x in link for x in ['/p/', '/product/']) and not re.search(r'\d{5,}', link): return False
        if len(title) < 5 or title.lower().startswith('www'): return False
        return True

    queries = [f"site:smythstoys.com {keyword}"]
   
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_smyth_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_smyth_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

    return enrich_products_parallel(session, deduplicate(products)[:30], 'smythtoys')


def scrape_generic(session, shop_key, domain, keyword):
    products = []
   
    def valid_generic_link(link, title):
        if domain not in link: return False
        if link.rstrip('/').endswith(domain): return False
        if '/impressum' in link or '/datenschutz' in link: return False
        if len(title) < 5 or title.lower().startswith('www'): return False
        return True

    queries = [f"site:{domain} {keyword}"]
   
    for q in queries:
        try:
            res = session.post("https://lite.duckduckgo.com/lite/", data={'q': q}, timeout=8)
            if res.status_code == 200:
                soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                for a in soup.find_all('a', href=True):
                    link = a['href']
                    if 'uddg=' in link:
                        try: link = urllib.parse.unquote(re.search(r'uddg=([^&]+)', link).group(1))
                        except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_generic_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
        except Exception: pass
        if len(products) >= 15: break

    if len(products) < 10:
        for q in queries:
            try:
                res = session.get(f"https://de.search.yahoo.com/search?p={urllib.parse.quote(q)}", timeout=8)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.content.decode('utf-8', 'ignore'), 'html.parser')
                    for a in soup.find_all('a', href=True):
                        link = a['href']
                        if 'RU=' in link:
                            try: link = urllib.parse.unquote(link.split('RU=')[1].split('/RK=')[0])
                            except: pass
                        link = link.split('?')[0]
                        t = re.sub(r'\s*[:|-|•]\s*.*$', '', a.get_text(strip=True)).strip()
                        if valid_generic_link(link, t):
                            products.append({"title": t, "price": "-", "imageUrl": "", "link": link})
            except Exception: pass
            if len(products) >= 15: break

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
   
    if shop_key == 'amazon': products = scrape_amazon(session, keyword)
    elif shop_key == 'norma': products = scrape_norma(session, keyword)
    elif shop_key == 'netto': products = scrape_netto(session, keyword)
    elif shop_key == 'kaufland': products = scrape_kaufland(session, keyword)
    elif shop_key == 'otto': products = scrape_otto(session, keyword)
    elif shop_key == 'smythtoys': products = scrape_smythtoys(session, keyword)
    else: products = scrape_generic(session, shop_key, SHOP_DOMAINS[shop_key], keyword)

    return jsonify({
        "status": "success", "shop": shop_key, "domain": SHOP_DOMAINS[shop_key],
        "keyword": keyword, "count": len(products), "products": products
    })

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
