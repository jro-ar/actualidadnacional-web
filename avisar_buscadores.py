"""Avisa a Bing (y a los buscadores que usan IndexNow) de las notas nuevas de
actualidadnacional.net, para que aparezcan rapido en Bing, Edge, Copilot, Yahoo y DuckDuckGo.
Lee el mapa del sitio, manda solo las notas que todavia no se avisaron y anota cuales mando.
Nunca hace fallar la corrida: si algo sale mal, solo lo informa."""
import json
import re
import requests

HOST = "actualidadnacional.net"
KEY = "6a499088b2c88489102337f22535ab30"
REGISTRO = "indexnow_enviadas.json"


def main():
    try:
        with open(REGISTRO, encoding="utf-8") as f:
            enviadas = json.load(f)
    except Exception:
        enviadas = []
    try:
        xml = requests.get(f"https://{HOST}/sitemap.aspx", timeout=30,
                           headers={"User-Agent": "ActualidadNacionalBot/1.0"}).text
        locs = re.findall(r"<loc>(https://actualidadnacional\.net/\d+~[^<]+)</loc>", xml)
        locs = [l.replace("&amp;", "&") for l in locs]
        nuevas = [u for u in locs if u not in enviadas][:100]
        if not nuevas:
            print("IndexNow: no hay notas nuevas para avisar")
            return
        r = requests.post("https://api.indexnow.org/indexnow", timeout=30, json={
            "host": HOST,
            "key": KEY,
            "keyLocation": f"https://{HOST}/{KEY}.txt",
            "urlList": nuevas,
        })
        print(f"IndexNow: {len(nuevas)} notas avisadas -> respuesta {r.status_code}")
        if r.status_code in (200, 202):
            enviadas = (enviadas + nuevas)[-500:]
    except Exception as e:
        print(f"IndexNow: no se pudo avisar ({e})")
    with open(REGISTRO, "w", encoding="utf-8") as f:
        json.dump(enviadas, f, ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
