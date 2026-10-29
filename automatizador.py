import requests
from bs4 import BeautifulSoup
import anthropic
import os, re, time, json, base64
from datetime import datetime
from io import BytesIO
from PIL import Image

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PANEL_URL = "https://www.actualidadnacional.net/panel/panel_listado.aspx"
PANEL_FORM_URL = "https://www.actualidadnacional.net/panel/panel_agregar_nota.aspx"
PANEL_USER        = os.environ.get("ACTUALIDAD_PANEL_USER", "")
PANEL_PASS        = os.environ.get("ACTUALIDAD_PANEL_PASS", "")
if not PANEL_USER or not PANEL_PASS:
    raise SystemExit("Faltan los secretos ACTUALIDAD_PANEL_USER / ACTUALIDAD_PANEL_PASS en GitHub")

cliente_anthropic = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-AR,es;q=0.9",
}

imagenes_usadas = set()

# Registro de notas ya publicadas (para no repetir la misma noticia en corridas seguidas)
ARCHIVO_PUBLICADAS = "publicadas.json"
try:
    with open(ARCHIVO_PUBLICADAS, encoding="utf-8") as _f:
        URLS_PUBLICADAS = json.load(_f)
except Exception:
    URLS_PUBLICADAS = []


def guardar_publicadas():
    try:
        with open(ARCHIVO_PUBLICADAS, "w", encoding="utf-8") as _f:
            json.dump(URLS_PUBLICADAS[-500:], _f, ensure_ascii=False, indent=0)
    except Exception as e:
        print(f"  ⚠️ No se pudo guardar {ARCHIVO_PUBLICADAS}: {e}")


def registrar_publicada(url):
    if url and url not in URLS_PUBLICADAS:
        URLS_PUBLICADAS.append(url)
        guardar_publicadas()

# Lista ampliada: incluye artículos, preposiciones, pronombres Y adjetivos
# que nunca pueden terminar un título (siempre necesitan algo después)
PALABRAS_MALAS = {
    # artículos y determinantes
    'de', 'del', 'la', 'el', 'los', 'las', 'un', 'una', 'unos', 'unas',
    # preposiciones
    'y', 'o', 'a', 'en', 'que', 'con', 'por', 'para', 'ante', 'bajo',
    'cabe', 'hacia', 'hasta', 'sobre', 'tras', 'al', 'ni',
    # pronombres posesivos/demostrativos (preceden a un sustantivo)
    'su', 'sus', 'mi', 'mis', 'tu', 'tus', 'nuestro', 'nuestra',
    'nuestros', 'nuestras', 'este', 'esta', 'estos', 'estas',
    'ese', 'esa', 'esos', 'esas', 'aquel', 'aquella',
    # pronombres/partículas
    'se', 'no', 'si',
    # adjetivos que SIEMPRE preceden o acompañan a un sustantivo
    # (nunca cierran una idea solos)
    'propio', 'propia', 'propios', 'propias',
    'mismo', 'misma', 'mismos', 'mismas',
    'nuevo', 'nueva', 'nuevos', 'nuevas',
    'gran', 'grande', 'grandes',
    'primer', 'primera', 'primeros', 'primeras',
    'último', 'última', 'últimos', 'últimas',
    'mayor', 'menor', 'peor', 'mejor',
    'otro', 'otra', 'otros', 'otras',
    'cada', 'todo', 'toda', 'todos', 'todas',
    'cierto', 'cierta', 'ciertos', 'ciertas',
    'algún', 'alguna', 'algunos', 'algunas',
    'ningún', 'ninguna',
}


def cortar_texto(texto, max_chars):
    """Corta el texto al límite de caracteres, eliminando
    palabras incompletas al final (artículos, preposiciones, adjetivos)."""
    if len(texto) <= max_chars:
        return texto
    cortado = texto[:max_chars].rsplit(' ', 1)[0]
    # Eliminar palabras malas del final en loop hasta encontrar un cierre válido
    for _ in range(20):  # máximo 20 iteraciones para no loop infinito
        partes = cortado.rsplit(' ', 1)
        if len(partes) > 1 and partes[-1].lower().rstrip('.,;:') in PALABRAS_MALAS:
            cortado = partes[0]
        else:
            break
    return cortado


def titulo_es_completo(titulo):
    """Verifica que el título no termine en una palabra que deja la frase abierta."""
    if not titulo:
        return False
    ultima = titulo.rsplit(' ', 1)[-1].lower().rstrip('.,;:')
    return ultima not in PALABRAS_MALAS


def acortar_con_ia(texto, max_chars, tipo="título"):
    """Usa IA para acortar el texto. Pide explícitamente una frase completa."""
    prompt = f"""El siguiente {tipo} tiene {len(texto)} caracteres y debe tener MENOS de {max_chars} caracteres.

Reescribilo MÁS CORTO con MÁXIMO {max_chars} caracteres.
OBLIGATORIO: debe ser una oración COMPLETA que termine en un SUSTANTIVO o VERBO.
PROHIBIDO terminar con artículos (el, la, los), preposiciones (de, en, por, con),
pronombres posesivos (su, mi, tu) ni adjetivos sin su sustantivo (propio, mismo, nuevo, etc.).

Ejemplo MALO: "La izquierda busca capitalizar el descontento pero enfrenta su propio"  ← incompleto
Ejemplo BUENO: "La izquierda no puede capitalizar el descontento"  ← completo

Responde SOLO con el texto corregido, sin comillas ni explicaciones.

{tipo.upper()}: {texto}"""
    resp = cliente_anthropic.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}]
    )
    resultado = resp.content[0].text.strip()
    # Corte de seguridad con la lista ampliada
    return cortar_texto(resultado, max_chars)


def extraer_imagen_meta(soup):
    for prop in ["og:image", "twitter:image"]:
        meta = soup.find("meta", property=prop) or soup.find("meta", {"name": prop})
        if meta and meta.get("content"):
            src = meta["content"]
            if "logo" not in src.lower() and len(src) > 50:
                return src
    return None


def extraer_imagen_contenido(soup):
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy") or ""
        if not src.startswith("http"): continue
        if "logo" in src.lower(): continue
        if not any(ext in src.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"]): continue
        if len(src) < 50: continue
        return src
    return None


def extraer_parrafos(soup):
    parrafos = []
    for sel in [".article-body", ".body-article", "[data-type='text']", ".content-body", ".nota-body"]:
        c = soup.select_one(sel)
        if c:
            for p in c.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 40: parrafos.append(t)
            break
    if not parrafos:
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if t and len(t) > 60: parrafos.append(t)
    return parrafos


def obtener_noticias_infobae(cantidad=6):
    print("📰 Scrapeando Infobae Política...")
    r = requests.get("https://www.infobae.com/politica/", headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    articulos = []
    vistos = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.infobae.com" + href
        if "/politica/" not in href: continue
        if href in vistos or href in URLS_PUBLICADAS or len(href) < 40: continue
        if any(x in href for x in ["#", "autor", "tag", "categoria"]): continue
        vistos.add(href)
        articulos.append(href)
        if len(articulos) >= cantidad: break
    print(f"  Encontré {len(articulos)} URLs.")
    return articulos


def extraer_nota_infobae(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "lxml")
        titulo = ""
        for sel in ["h1.article-headline", "h1"]:
            t = soup.select_one(sel)
            if t: titulo = t.get_text(strip=True); break
        bajada = ""
        for sel in [".article-subheadline", "h2", ".summary"]:
            b = soup.select_one(sel)
            if b: bajada = b.get_text(strip=True); break
        parrafos = extraer_parrafos(soup)
        img_url = extraer_imagen_meta(soup) or extraer_imagen_contenido(soup)
        return {
            "url": url,
            "titulo_original": titulo,
            "bajada_original": bajada,
            "cuerpo_original": "\n\n".join(parrafos[:12]),
            "img_url": img_url,
        }
    except Exception as e:
        print(f"  ⚠️ Error: {e}"); return None


def obtener_nota_tn():
    print("📰 Scrapeando TN Economía...")
    r = requests.get("https://tn.com.ar/economia/", headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    vistos = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://tn.com.ar" + href
        if "/economia/" not in href: continue
        if href in vistos or href in URLS_PUBLICADAS or len(href) < 40: continue
        if any(x in href for x in ["#", "autor", "tag", "categoria"]): continue
        vistos.add(href)
        try:
            r2 = requests.get(href, headers=HEADERS, timeout=15)
            soup2 = BeautifulSoup(r2.text, "lxml")
            titulo = ""
            for sel in ["h1"]:
                t = soup2.select_one(sel)
                if t: titulo = t.get_text(strip=True); break
            if not titulo: continue
            bajada = ""
            for sel in ["h2", ".summary", ".lead"]:
                b = soup2.select_one(sel)
                if b: bajada = b.get_text(strip=True); break
            parrafos = extraer_parrafos(soup2)
            if not parrafos: continue
            img_url = extraer_imagen_meta(soup2) or extraer_imagen_contenido(soup2)
            print(f"  ✅ Encontré nota TN: {titulo[:60]}")
            return {
                "url": href,
                "titulo_original": titulo,
                "bajada_original": bajada,
                "cuerpo_original": "\n\n".join(parrafos[:12]),
                "img_url": img_url,
            }
        except: continue
    return None


def obtener_nota_deportes_infobae():
    print("📰 Scrapeando Infobae Deportes...")
    r = requests.get("https://www.infobae.com/deportes/", headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    vistos = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            href = "https://www.infobae.com" + href
        if "/deportes/" not in href: continue
        if href in vistos or href in URLS_PUBLICADAS or len(href) < 40: continue
        if any(x in href for x in ["#", "autor", "tag", "categoria"]): continue
        vistos.add(href)
        try:
            r2 = requests.get(href, headers=HEADERS, timeout=15)
            soup2 = BeautifulSoup(r2.text, "lxml")
            titulo = ""
            for sel in ["h1.article-headline", "h1"]:
                t = soup2.select_one(sel)
                if t: titulo = t.get_text(strip=True); break
            if not titulo or len(titulo) < 10: continue
            bajada = ""
            for sel in [".article-subheadline", "h2", ".summary"]:
                b = soup2.select_one(sel)
                if b: bajada = b.get_text(strip=True); break
            parrafos = extraer_parrafos(soup2)
            if not parrafos: continue
            img_url = extraer_imagen_meta(soup2) or extraer_imagen_contenido(soup2)
            if not img_url: continue
            print(f"  ✅ Encontré nota deportes: {titulo[:60]}")
            return {
                "url": href,
                "titulo_original": titulo,
                "bajada_original": bajada,
                "cuerpo_original": "\n\n".join(parrafos[:12]),
                "img_url": img_url,
            }
        except: continue
    return None


def limpiar_fuente(texto):
    for medio in ["Infobae", "TN", "Todo Noticias", "Olé", "Ole", "Clarín", "Clarin"]:
        texto = re.sub(rf'[Cc]omo (contó|dijo|informó|publicó|señaló) {medio}[,.]?', '', texto)
        texto = re.sub(rf'[Ss]egún {medio}[,.]?', '', texto)
        texto = re.sub(medio, '', texto)
    return texto


def corregir_conjunciones(texto: str) -> str:
    """
    Corrige conjunciones segun ortografia espanola:
    - 'o' -> 'u' antes de palabras que empiezan con 'o' u 'ho'
    - 'y' -> 'e' antes de palabras que empiezan con 'i' u 'hi'
    """
    texto = re.sub(r'\bo\b(?=\s+[oO])', 'u', texto)
    texto = re.sub(r'\bO\b(?=\s+[oO])', 'U', texto)
    texto = re.sub(r'\bo\b(?=\s+[hH][oO])', 'u', texto)
    texto = re.sub(r'\bO\b(?=\s+[hH][oO])', 'U', texto)
    texto = re.sub(r'\by\b(?=\s+[iI])', 'e', texto)
    texto = re.sub(r'\bY\b(?=\s+[iI])', 'E', texto)
    texto = re.sub(r'\by\b(?=\s+[hH][iI])', 'e', texto)
    texto = re.sub(r'\bY\b(?=\s+[hH][iI])', 'E', texto)
    return texto


def reescribir_con_ia(nota, max_titulo=63, max_bajada=170):
    cuerpo_limpio = limpiar_fuente(nota['cuerpo_original'])
    # Buffer ampliado: pedimos un límite MÁS ESTRICTO a Claude
    # para que el resultado tenga más margen y no llegue al tope
    limite_titulo = max_titulo - 20  # política: 43, eco/dep: 25
    limite_bajada = max_bajada - 20  # política: 150, eco/dep: 64

    prompt = f"""Sos un periodista argentino. Reescribi esta noticia con tus propias palabras.

REGLAS ESTRICTAS:
- TITULO: MÁXIMO {limite_titulo} caracteres. Frase periodística corta y COMPLETA.
  OBLIGATORIO: debe terminar en un SUSTANTIVO o VERBO.
  PROHIBIDO terminar con: artículos (el, la, los), preposiciones (de, en, por),
  pronombres posesivos (su, mi, tu, nuestro), adjetivos sin sustantivo (propio, mismo, nuevo, gran, etc.).
  Ejemplo MALO: "La oposición busca unidad pero enfrenta su propio"  ← incompleto, falta sustantivo
  Ejemplo BUENO: "La oposición busca unidad pero no logra acuerdo"  ← completo
- BAJADA: MÁXIMO {limite_bajada} caracteres. Frase completa con sentido.
- TEXTO: mínimo 300 palabras, varios párrafos, conserva todos los datos.
- No copies frases textuales. No menciones medios de comunicación.
- ORTOGRAFIA: Usa 'u' en lugar de 'o' ante palabras que empiecen con 'o' u 'ho'. Usa 'e' en lugar de 'y' ante palabras que empiecen con 'i' u 'hi'.

Respondé EXACTAMENTE con este formato, sin nada antes ni después:
<titulo>título</titulo>
<bajada>bajada</bajada>
<texto>texto de la nota</texto>

ORIGINAL:
Titulo: {nota['titulo_original']}
Bajada: {nota['bajada_original']}
Texto: {cuerpo_limpio[:4000]}"""

    datos = None
    for intento in range(3):
        try:
            resp = cliente_anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}]
            )
            crudo = resp.content[0].text
            datos = {}
            for campo in ("titulo", "bajada", "texto"):
                m = re.search(rf"<{campo}>(.*?)</{campo}>", crudo, re.S)
                datos[campo] = m.group(1).strip() if m else ""
            if all(datos.values()):
                break
            print(f"  ⚠️ Respuesta de IA incompleta (intento {intento + 1}), reintentando...")
            datos = None
        except Exception as e:
            print(f"  ⚠️ Error IA (intento {intento + 1}): {e}")
            datos = None
            time.sleep(3)
    if not datos:
        raise Exception("la IA no devolvió una reescritura válida tras 3 intentos")
    titulo = datos["titulo"]
    bajada = datos["bajada"]

    # --- Validación y corrección del TÍTULO ---
    # Paso 1: si supera el límite máximo, pedir a IA que lo acorte
    if len(titulo) > max_titulo:
        titulo = acortar_con_ia(titulo, max_titulo, "título")

    # Paso 2: corte mecánico de seguridad con lista ampliada
    titulo = cortar_texto(titulo, max_titulo)

    # Paso 3 (NUEVO): verificación de completitud semántica
    # Si aún termina en palabra mala, pedir a IA que lo reescriba completo
    if not titulo_es_completo(titulo):
        print(f"  ⚠️ Título incompleto detectado: '{titulo}' → corrigiendo con IA...")
        titulo = acortar_con_ia(titulo, max_titulo, "título")
        titulo = cortar_texto(titulo, max_titulo)

    # --- Validación y corrección de la BAJADA ---
    if len(bajada) > max_bajada:
        bajada = acortar_con_ia(bajada, max_bajada, "bajada")
    bajada = cortar_texto(bajada, max_bajada)

    titulo = corregir_conjunciones(titulo)
    bajada = corregir_conjunciones(bajada)
    datos["texto"] = corregir_conjunciones(datos["texto"])
    titulo, bajada, datos["texto"] = revisar_ortografia(titulo, bajada, datos["texto"], max_titulo, max_bajada)
    print(f"  ✅ Título final ({len(titulo)} chars): {titulo}")
    return {**nota, "titulo": titulo, "bajada": bajada, "texto": datos["texto"]}


def revisar_ortografia(titulo, bajada, texto, max_titulo, max_bajada):
    """Revision ortografica final (normas RAE) antes de publicar. Solo corrige errores, no reescribe."""
    import difflib
    prompt = f"""Sos un corrector ortográfico profesional de un diario argentino. Revisá el TÍTULO, la BAJADA y el TEXTO de abajo y corregí TODOS los errores de ortografía según las normas de la RAE.

Prestá especial atención a:
- Tildes diacríticas: sí (afirmación) / si (condicional); más (cantidad) / mas (pero); él (pronombre) / el (artículo); tú / tu; mí / mi; sé / se; dé / de; té / te; aún (todavía) / aun (incluso).
- Interrogativos y exclamativos con tilde, también en preguntas indirectas: qué, cómo, cuándo, dónde, quién, cuál, cuánto.
- Tildes en palabras agudas, graves y esdrújulas (por ejemplo: ratificó, político, según, después).
- Mayúsculas en nombres propios y sin mayúsculas innecesarias.
- Puntuación, signos de apertura (¿ ¡), comillas y espacios dobles.
- Concordancia de género y número, y errores de tipeo.

REGLAS:
- Corregí SOLO errores. No cambies palabras, estilo, orden ni sentido si están bien escritas.
- No agregues ni quites información. No alargues el título ni la bajada.
- Si no hay errores, devolvé el texto exactamente igual.

Respondé EXACTAMENTE con este formato, sin nada antes ni después:
<titulo>título corregido</titulo>
<bajada>bajada corregida</bajada>
<texto>texto corregido</texto>
<correcciones>una corrección por línea, o "ninguna"</correcciones>

TÍTULO: {titulo}
BAJADA: {bajada}
TEXTO:
{texto}"""

    def extraer(etiqueta, crudo):
        m = re.search(rf"<{etiqueta}>(.*?)</{etiqueta}>", crudo, re.S)
        return m.group(1).strip() if m else None

    crudo = None
    for intento in range(3):
        try:
            resp = cliente_anthropic.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=6000,
                messages=[{"role": "user", "content": prompt}],
            )
            crudo = resp.content[0].text
            if extraer("titulo", crudo) is not None:
                break
            print(f"  ⚠️ Revisión ortográfica: respuesta sin formato (intento {intento + 1})")
            crudo = None
        except Exception as e:
            print(f"  ⚠️ Revisión ortográfica falló (intento {intento + 1}): {e}")
            time.sleep(3)
    if not crudo:
        return titulo, bajada, texto

    def aceptar(original, corregido, limite=None, minimo=0.85):
        if not isinstance(corregido, str) or not corregido.strip():
            return original
        corregido = corregido.strip()
        if limite and len(corregido) > limite:
            return original
        if difflib.SequenceMatcher(None, original, corregido).ratio() < minimo:
            return original
        return corregido

    nuevo_titulo = aceptar(titulo, extraer("titulo", crudo), max_titulo)
    nueva_bajada = aceptar(bajada, extraer("bajada", crudo), max_bajada)
    nuevo_texto = aceptar(texto, extraer("texto", crudo), None, 0.9)
    correcciones = extraer("correcciones", crudo) or ""
    for c in correcciones.splitlines():
        c = c.strip(" -•\t")
        if c and c.lower() != "ninguna":
            print(f"  ✏️ Ortografía: {c}")
    if nuevo_titulo != titulo:
        print(f"  ✏️ Título corregido: '{titulo}' → '{nuevo_titulo}'")
    print("  ✅ Revisión ortográfica OK")
    return nuevo_titulo, nueva_bajada, nuevo_texto


def descargar_imagen(img_url, nombre):
    if not img_url: return None
    if img_url in imagenes_usadas:
        print(f"  ⚠️ Imagen ya usada, saltando...")
        return None
    try:
        r = requests.get(img_url, headers=HEADERS, timeout=15)
        img = Image.open(BytesIO(r.content)).convert("RGB")
        # 1200 px de ancho: es el minimo que pide Google Discover para mostrar fotos grandes
        if img.width > 1200:
            ratio = 1200 / img.width
            img = img.resize((1200, int(img.height * ratio)), Image.LANCZOS)
        ruta = f"/tmp/{nombre}.jpg"
        img.save(ruta, "JPEG", quality=85)
        imagenes_usadas.add(img_url)
        return ruta
    except Exception as e:
        print(f"  ⚠️ Sin imagen: {e}"); return None


def hacer_login():
    session = requests.Session()
    session.headers.update(HEADERS)
    r = session.get("https://www.actualidadnacional.net/panel/login.aspx", timeout=15)
    if r.status_code != 200:
        r = session.get(PANEL_URL, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    vs = soup.find("input", {"id": "__VIEWSTATE"})
    vsg = soup.find("input", {"id": "__VIEWSTATEGENERATOR"})
    ev = soup.find("input", {"id": "__EVENTVALIDATION"})
    uf = soup.find("input", {"type": "text"})
    pf = soup.find("input", {"type": "password"})
    sb = soup.find("input", {"type": "submit"}) or soup.find("input", {"type": "image"})
    payload = {
        "__VIEWSTATE": vs["value"] if vs else "",
        "__VIEWSTATEGENERATOR": vsg["value"] if vsg else "",
        "__EVENTVALIDATION": ev["value"] if ev else "",
        uf["name"] if uf else "txtUser": PANEL_USER,
        pf["name"] if pf else "txtPass": PANEL_PASS,
        sb["name"] if sb else "btnLogin": "Ingresar",
    }
    r2 = session.post(r.url, data=payload, timeout=15)
    if "PANEL" in r2.text or "Agregar" in r2.text:
        print("✅ Login exitoso")
    else:
        print("⚠️ Login no confirmado, continúo igual...")
    return session


def publicar_nota(session, nota, ruta_imagen, orden):
    r = session.get(PANEL_FORM_URL, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    vs = soup.find("input", {"id": "__VIEWSTATE"})["value"]
    vsg = soup.find("input", {"id": "__VIEWSTATEGENERATOR"})["value"]
    ev = soup.find("input", {"id": "__EVENTVALIDATION"})["value"]
    hoy = datetime.now()
    data = {
        "__VIEWSTATE": vs, "__VIEWSTATEGENERATOR": vsg, "__EVENTVALIDATION": ev,
        "radio": str(orden),
        "drpDay": hoy.strftime("%d"), "drpMonth": hoy.strftime("%m"), "drpYear": str(hoy.year),
        "txtTitle": nota["titulo"], "txtSubTitle": nota["bajada"],
        "txtDescriptionLong": nota["texto"],
        "imgPublicar.x": "98", "imgPublicar.y": "22",
    }
    files = None
    if ruta_imagen and os.path.exists(ruta_imagen):
        files = {"flUpload": (os.path.basename(ruta_imagen), open(ruta_imagen, "rb"), "image/jpeg")}
    r2 = session.post(PANEL_FORM_URL, data=data, files=files, timeout=30)
    if files: files["flUpload"][1].close()
    return r2.status_code == 200


def procesar_nota(nota, session, orden, max_titulo, max_bajada, idx):
    if not nota or not nota["titulo_original"]:
        print("  ⚠️ Sin nota. Saltando...")
        return False
    print(f"  ✅ Extraída: {nota['titulo_original'][:60]}")
    try:
        nota = reescribir_con_ia(nota, max_titulo=max_titulo, max_bajada=max_bajada)
        print(f"  ✅ Reescrita: {nota['titulo']}")
    except Exception as e:
        # Nunca publicar el original cortado ni copiado: si la IA falla, la nota se saltea
        print(f"  ⚠️ Error IA: {e}. Nota NO publicada (no se publica el original cortado).")
        return False
    ruta_img = descargar_imagen(nota.get("img_url"), f"nota_{idx}_{int(time.time())}")
    print("  ✅ Imagen OK" if ruta_img else "  ⚠️ Sin imagen.")
    if publicar_nota(session, nota, ruta_img, orden):
        print(f"  ✅ Publicada en orden {orden}!")
        registrar_publicada(nota.get("url"))
        return True
    else:
        print("  ❌ Error al publicar.")
        return False


def ejecutar_automatizacion():
    print("=" * 55)
    print("🗞️ AUTOMATIZADOR — ActualidadNacional")
    print(f"  Fecha: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    print("=" * 55)

    session = hacer_login()
    publicadas = 0

    print("\n📌 SECCIÓN POLÍTICA")
    urls = obtener_noticias_infobae(6)
    for i, url in enumerate(urls, 1):
        print(f"\n── Noticia política {i}/6")
        nota = extraer_nota_infobae(url)
        if procesar_nota(nota, session, orden=1, max_titulo=63, max_bajada=170, idx=f"pol_{i}"):
            publicadas += 1
        time.sleep(3)

    print("\n📌 SECCIÓN ECONOMÍA")
    nota_eco = obtener_nota_tn()
    if procesar_nota(nota_eco, session, orden=4, max_titulo=45, max_bajada=84, idx="eco"):
        publicadas += 1
    time.sleep(3)

    print("\n📌 SECCIÓN DEPORTES")
    nota_dep = obtener_nota_deportes_infobae()
    if procesar_nota(nota_dep, session, orden=5, max_titulo=45, max_bajada=84, idx="dep"):
        publicadas += 1

    print(f"\n{'='*55}")
    print(f"🎉 TERMINADO: {publicadas} noticias publicadas")
    print("=" * 55)


if __name__ == "__main__":
    ejecutar_automatizacion()
