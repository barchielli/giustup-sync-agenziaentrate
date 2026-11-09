#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Giustup — la prassi dell'Agenzia delle Entrate nel motore di ricerca.

Porta nell'archivio di giustup.it tutta la prassi che l'Agenzia delle Entrate
pubblica nella sezione «Normativa e prassi»: le CIRCCOLARI, le RISOLUZIONI
(che il portale chiama anche le «decisioni»), i PROVVEDIMENTI del Direttore
soggetti a pubblicità legale (le «deliberazioni») e le RISPOSTE alle istanze
di interpello e di consulenza giuridica (i «pareri»). Il documento si scarica
in PDF, il testo si estrae con `pdftotext` e resta su S3; i metadati e il testo
vanno nell'indice `giustup-agenziaentrate`, che è un indice suo perché questa
è prassi tributaria, non giurisprudenza, e non ha i campi delle sentenze.

Perché un indice separato: come per ANAC e per la Corte dei conti, l'archivio
vive da solo nel suo indice, e la ricerca del sito e la scheda del provvedimento
leggono il contratto dei campi comune. La categoria distingue le quattro
famiglie dentro lo stesso archivio: circolare, risoluzione, provvedimento,
interpello (con sotto-famiglie «principio di diritto» e «consulenza»).

COME LAVORA

1. Il portale organizza ogni famiglia in una gerarchia di pagine: la sezione
   di arrivo elenca le annate, l'annata elenca i mesi, il mese contiene gli
   atti. Ogni riga della gerarchia è un blocco `indcart_gn`: chi porta un PDF
   è un atto, chi porta un indirizzo `/portale/...` è il rinvio a una pagina
   più in basso. Il sync cammina questa gerarchia da una lista di radici per
   famiglia (l'archivio e la pagina corrente).
2. Si paginano solo gli atti nuovi: la pagina che non porta niente di nuovo
   chiude il ramo. Il giro della notte costa poco e lo storico riprende da
   dove era rimasto.
3. Il PDF si scarica, si mette su S3 in `contentstore/agenziaentrate/<anno>/<id>.pdf`
   e si legge con `pdftotext -layout`.
4. Il documento va in indice con `_id` = UUIDv5 dell'indirizzo del PDF:
   rilanciare il sync aggiorna lo stesso documento invece di duplicarlo.
5. Le famiglie non sono scritte a mano per ogni atto: si portano nei metadati
   della categoria, e una categoria nuova entra dal nome che la pagina usa.

USO

    python3 agenziaentrate.py --famiglie        # elenca le famiglie ed esce
    python3 agenziaentrate.py --prova 3         # tre atti per famiglia, non scrive
    python3 agenziaentrate.py --massimo 900     # il giro della notte
    python3 agenziaentrate.py --categoria circolari --prova 2
    python3 agenziaentrate.py --tutto --massimo 30000  # il recupero dello storico
    python3 agenziaentrate.py --anno 2024 --massimo 2000  # solo un'annata

Codici di uscita: 0 tutto fatto; 1 qualche documento non è arrivato (resta
nello stato, si riprende la notte dopo); 2 il portale non rispondeva più.
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3
from lxml import html as LH

# ---------------------------------------------------------------- costanti

BASE = "https://www.agenziaentrate.gov.it"
PORTALE = BASE + "/portale"

OS_BASE = os.environ.get("GIUSTUP_OPENSEARCH", "https://localhost:9200")
IDX = os.environ.get("GIUSTUP_INDICE", "giustup-agenziaentrate")
BUCKET = os.environ.get("GIUSTUP_BUCKET", "giustup-archivio-sentenze-593859982945")
PREFISSO_PDF = "contentstore/agenziaentrate"
CHIAVE_STATO = "stato-sync/agenziaentrate.json"
PREFISSO_REC = os.environ.get("GIUSTUP_PREFISSO_REC", "contentstore/agenziaentrate/_rec")

# Scissione Milano/motore: quando è vero, il giro (da IP italiano, runner di
# Milano) salva su S3 anche il record JSON del documento, oltre al PDF, così il
# motore lo indicizza in OpenSearch senza rifare la fetch dalla fonte.
_salva_record = False

CERT = "/etc/opensearch/kirk.pem"
CHIAVE_CERT = "/etc/opensearch/kirk-key.pem"

TESTATE = {
    "User-Agent": "Mozilla/5.0 (compatible; GiustupSync/1.0; +https://giustup.it)",
    "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
}

PAUSA_FRA_PAGINE = 0.5
PAUSA_FRA_PDF = 1.0
PAUSA_OGNI = 20
PAUSA_LUNGA = 2.0

TENTATIVI = 5
RIPROVABILI = (425, 429, 500, 502, 503, 504)

# Attenzione: NON mettere la cifra "404" fra le parole d'errore. Gli indirizzi
# dei documenti dell'Agenzia (le UDA li portano nel percorso) contengono "404"
# in modo legittimo, e il documento inesistente si riconosce dal PDF che non
# arriva (non comincia con %PDF) o dal corpo HTML della pagina d'errore.
PAROLE_ERRORE = ("page not found", "pagina non trovata", "errore 404")

RIGHE_PER_LOTTO = 25
FALLIMENTI_DI_FILA = 25

NOMESPACE = uuid.UUID("d3a8f6c2-5b41-4e60-8c2d-9f1a7b3e6d05")
AUTORITA = "Agenzia delle Entrate"

RE_NUMERO = re.compile(r"\bn\.?\s*(\d{1,6})\b", re.I)
RE_DATA_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b")
RE_DATA_ESTESA = re.compile(
    r"\b(\d{1,2})\s+([a-zàèéìòù]+)\s+(\d{4})\b", re.I)

FAMIGLIE_NOTE = {
    "circolari": {
        "categoria": "circolare",
        "nome": "Circolari",
        "radici": [
            PORTALE + "/normativa-e-prassi/circolari",
            PORTALE + "/normativa-e-prassi/circolari/archivio-circolari",
        ],
    },
    "risoluzioni": {
        "categoria": "risoluzione",
        "nome": "Risoluzioni",
        "radici": [
            PORTALE + "/normativa-e-prassi/risoluzioni",
            PORTALE + "/normativa-e-prassi/risoluzioni/archivio-risoluzioni",
        ],
    },
    "provvedimenti": {
        "categoria": "provvedimento",
        "nome": "Provvedimenti del Direttore",
        "radici": [
            PORTALE + "/normativa-e-prassi/provvedimenti",
            PORTALE + "/web/guest/archivio/normativa-prassi-archivio-documentazione/"
                      "provvedimenti/provvedimenti-soggetti",
        ],
    },
    "interpelli": {
        "categoria": "interpello",
        "nome": "Risposte agli interpelli e consulenza giuridica",
        "radici": [
            PORTALE + "/normativa-e-prassi/risposte-agli-interpelli",
            PORTALE + "/normativa-e-prassi/risposte-agli-interpelli/interpelli",
            PORTALE + "/normativa-e-prassi/risposte-agli-interpelli/interpelli/"
                      "archivio-interpelli",
        ],
    },
}

NOMESPACE = uuid.UUID("d3a8f6c2-5b41-4e60-8c2d-9f1a7b3e6d05")
AUTORITA = "Agenzia delle Entrate"

RE_ANNO_IN_URL = re.compile(r"(20\d{2}|19\d{2})")


def adesso():
    return datetime.datetime.now(datetime.timezone.utc)


def log(msg):
    print("%s %s" % (adesso().strftime("%Y-%m-%dT%H:%M:%SZ"), msg), flush=True)


def pulisci(testo):
    if not testo:
        return ""
    t = testo.replace("\xa0", " ").replace("\u200b", "").replace("\u00a0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    return t.strip()


def senza_accenti(testo):
    return "".join(c for c in unicodedata.normalize("NFD", testo or "")
                   if unicodedata.category(c) != "Mn")


def data_italiana(testo):
    if not testo:
        return None
    t = pulisci(testo)
    m = RE_DATA_NUM.search(t)
    if m:
        try:
            return "%04d-%02d-%02d" % (int(m.group(3)), int(m.group(2)),
                                       int(m.group(1)))
        except ValueError:
            return None
    m = RE_DATA_ESTESA.search(t)
    if m:
        MESI = {"gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
                "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
                "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12}
        mese = MESI.get(m.group(2).lower())
        if mese:
            try:
                return "%04d-%02d-%02d" % (int(m.group(3)), mese,
                                           int(m.group(1)))
            except ValueError:
                return None
    return None


def numero_da(testo):
    m = RE_NUMERO.search(testo or "")
    return m.group(1) if m else ""


def _ctx():
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    try:
        c.load_cert_chain(CERT, CHIAVE_CERT)
    except Exception as e:
        log("certificato di OpenSearch non caricato (%r): si prova senza" % (e,))
    return c


def os_chiama(percorso, corpo=None, metodo=None):
    dati = json.dumps(corpo).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(OS_BASE + percorso, data=dati,
                                 method=metodo or ("POST" if dati else "GET"))
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, context=_ctx(), timeout=180) as r:
        testo = r.read()
    return json.loads(testo) if testo else {}


MAPPATURA = {
    "settings": {
        "index": {"number_of_shards": 1, "number_of_replicas": 0,
                  "refresh_interval": "30s", "codec": "best_compression"},
        "analysis": {
            "filter": {
                "it_elisione": {"type": "elision", "articles_case": True,
                                "articles": ["c", "l", "all", "dall", "dell", "nell",
                                             "sull", "coll", "pell", "gl", "agl", "dagl",
                                             "degl", "negl", "sugl", "un", "m", "t",
                                             "s", "v", "d"]},
                "it_vuote": {"type": "stop", "stopwords": "_italian_"},
                "it_radici": {"type": "stemmer", "language": "light_italian"},
            },
            "analyzer": {
                "italiano_giuridico": {"tokenizer": "standard",
                                       "filter": ["lowercase", "it_elisione", "it_vuote",
                                                  "it_radici", "asciifolding"]},
                "italiano_esatto": {"tokenizer": "standard",
                                    "filter": ["lowercase", "asciifolding"]},
            },
        },
    },
    "mappings": {
        "properties": {
            "id_nodo": {"type": "keyword"},
            "chiave_fonte": {"type": "keyword"},
            "fonte_url": {"type": "keyword", "index": False},
            "tipo": {"type": "keyword"},
            "autorita": {"type": "keyword"},
            "categoria": {"type": "keyword"},
            "categoria_nome": {"type": "keyword"},
            "famiglia": {"type": "keyword"},
            "titolo": {"type": "text", "analyzer": "italiano_giuridico",
                       "fields": {"esatto": {"type": "text", "analyzer": "italiano_esatto"}}},
            "oggetto": {"type": "text", "analyzer": "italiano_giuridico",
                        "fields": {"esatto": {"type": "text", "analyzer": "italiano_esatto"}}},
            "testo": {"type": "text", "analyzer": "italiano_giuridico",
                      "fields": {"esatto": {"type": "text", "analyzer": "italiano_esatto"}}},
            "numero": {"type": "keyword"},
            "anno": {"type": "short"},
            "data": {"type": "date"},
            "data_pubblicazione": {"type": "date"},
            "chiave_s3": {"type": "keyword", "index": False},
            "nome_file": {"type": "keyword", "index": False},
            "allegati": {"type": "keyword", "index": False},
            "caratteri": {"type": "integer"},
            "testo_scarso": {"type": "boolean"},
            "oscurato": {"type": "boolean"},
            "created": {"type": "date"},
            "impronta": {"type": "keyword", "index": False},
            "versione_pipeline": {"type": "keyword"},
        }
    },
}


def crea_indice_se_manca():
    esiste = False
    try:
        os_chiama("/%s" % IDX)
        esiste = True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if esiste:
        return False
    os_chiama("/%s" % IDX, MAPPATURA, "PUT")
    log("indice %s creato" % IDX)
    return True


def conosciuti(chiavi):
    if not chiavi:
        return set()
    trovati = set()
    chiavi = list(chiavi)
    for i in range(0, len(chiavi), 500):
        parte = chiavi[i:i + 500]
        corpo = {"size": len(parte), "_source": ["chiave_fonte"],
                 "query": {"terms": {"chiave_fonte": parte}}}
        esito = os_chiama("/%s/_search" % IDX, corpo)
        for h in esito.get("hits", {}).get("hits", []):
            fonte = (h.get("_source") or {}).get("chiave_fonte")
            if fonte:
                trovati.add(fonte)
    return trovati


def _bulk(righe):
    if not righe:
        return
    corpo = "\n".join(righe) + "\n"
    req = urllib.request.Request(OS_BASE + "/_bulk", data=corpo.encode("utf-8"),
                                 method="POST")
    req.add_header("Content-Type", "application/x-ndjson")
    with urllib.request.urlopen(req, context=_ctx(), timeout=300) as r:
        esito = json.loads(r.read() or b"{}")
    if esito.get("errors"):
        errori = [i for i in esito.get("items", [])
                  if list(i.values())[0].get("error")]
        raise RuntimeError("operazione in blocco: %d errori, il primo: %s"
                           % (len(errori), json.dumps(errori[:1], ensure_ascii=False)[:300]))


def indicizza(documenti):
    if not documenti:
        return 0
    righe = []
    for d in documenti:
        righe.append(json.dumps({"index": {"_index": IDX, "_id": d["id_nodo"]}}))
        righe.append(json.dumps(d, ensure_ascii=False))
    _bulk(righe)
    return len(documenti)


# ------------------------------------------------------------- S3

def s3():
    return boto3.client("s3")


def carica(cliente, chiave, corpo, tipo):
    cliente.put_object(Bucket=BUCKET, Key=chiave, Body=corpo, ContentType=tipo)


def salva_record(cliente, documento):
    """In scissione (Milano) il documento va su S3 anche come record JSON,
    così il motore lo indicizza senza rifare la fetch dalla fonte."""
    if not _salva_record or not documento:
        return
    anno = documento.get("anno") or 0
    chiave = "%s/%s/%s.json" % (PREFISSO_REC, anno, documento.get("id_nodo"))
    try:
        cliente.put_object(
            Bucket=BUCKET, Key=chiave,
            Body=json.dumps(documento, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json")
    except Exception as e:
        log("record %s non salvato su S3: %r" % (chiave, e))


def stato(riassunto):
    scritto = json.dumps(riassunto, ensure_ascii=False, indent=2)
    try:
        s3().put_object(Bucket=BUCKET, Key=CHIAVE_STATO,
                        Body=scritto.encode("utf-8"),
                        ContentType="application/json")
    except Exception as e:
        log("stato non scritto su S3: %r" % (e,))
    return scritto


def metrica(nome, valore):
    try:
        boto3.client("cloudwatch", region_name="eu-central-1").put_metric_data(
            Namespace="Giustup", MetricData=[
                {"MetricName": nome, "Value": valore, "Unit": "Count"}])
    except Exception as e:
        log("metrica %s non pubblicata: %r" % (nome, e))


# ------------------------------------------------------------ la scheda

def scheda(pdf_url, titolo, oggetto, categoria, voce_categoria, data=None,
            anno=None):
    chiave_fonte = pdf_url
    id_nodo = str(uuid.uuid5(NOMESPACE, "giustup:agenziaentrate:" + chiave_fonte))
    numero = numero_da(titolo)
    if not data:
        data = data_italiana(titolo) or data_italiana(oggetto)
    anno = anno or (int(data[:4]) if data else None)
    return {
        "id_nodo": id_nodo,
        "chiave_fonte": chiave_fonte,
        "fonte_url": chiave_fonte,
        "tipo": "agenziaentrate",
        "autorita": AUTORITA,
        "categoria": categoria,
        "categoria_nome": voce_categoria,
        "famiglia": categoria,
        "titolo": titolo,
        "oggetto": oggetto,
        "numero": numero,
        "anno": anno,
        "data": data,
        "data_pubblicazione": data,
        "chiave_s3": "",
        "nome_file": "",
        "allegati": [],
        "caratteri": 0,
        "testo_scarso": True,
        "oscurato": False,
        "created": adesso().isoformat().replace("+00:00", "Z"),
        "impronta": "",
        "versione_pipeline": "agenziaentrate-1.0",
    }


def lavora(pdf_url, titolo, oggetto, categoria, voce_categoria, scrivi,
           cliente_s3, data=None, anno=None):
    documento = scheda(pdf_url, titolo, oggetto, categoria, voce_categoria,
                       data, anno)
    if not documento["titolo"]:
        documento["titolo"] = os.path.basename(
            urllib.parse.urlparse(pdf_url).path) or "Provvedimento"
    if not documento["oggetto"]:
        documento["oggetto"] = documento["titolo"]

    corpo = richiesta(pdf_url, timeout=180)
    if corpo[:4] != b"%PDF":
        raise IOError("non è un PDF (%d byte)" % len(corpo))

    testo = testo_da_pdf(corpo)
    documento["testo"] = testo
    documento["caratteri"] = len(testo)
    documento["testo_scarso"] = len(testo) < 200
    documento["nome_file"] = os.path.basename(urllib.parse.urlparse(pdf_url).path)
    anno = documento["anno"] or 0
    documento["chiave_s3"] = "%s/%s/%s.pdf" % (PREFISSO_PDF, anno, documento["id_nodo"])
    documento["impronta"] = hashlib.sha256(corpo).hexdigest()

    if scrivi:
        carica(cliente_s3, documento["chiave_s3"], corpo, "application/pdf")
        salva_record(cliente_s3, documento)
    if documento["testo_scarso"]:
        log("    %s: testo estratto %d caratteri (PDF forse scansionato)"
            % (documento["titolo"][:70], len(testo)))
    return documento


def testo_da_pdf(corpo):
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(corpo)
        percorso = f.name
    try:
        esito = subprocess.run(["pdftotext", "-layout", percorso, "-"],
                               capture_output=True, timeout=180)
        return pulisci(esito.stdout.decode("utf-8", "replace"))
    finally:
        try:
            os.unlink(percorso)
        except OSError:
            pass


# ------------------------------------------------------------- il giro

def famiglie():
    elenco = sorted(FAMIGLIE_NOTE.items(),
                    key=lambda kv: (kv[1]["categoria"], kv[1]["nome"]))
    return elenco


def assoluto(href):
    """Rende assoluto un indirizzo del portale.

    Il sito dà i collegamenti in tre forme: assoluti (`https://...`), relativi
    alla radice (`/portale/...`) e relativi al portale (`/circolari-2026`).
    `scorri` li estrae così come sono; qui tornano alla forma intera."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/portale"):
        return BASE + href
    if href.startswith("/"):
        return PORTALE + href
    return PORTALE + "/" + href


def valuta_blocco(blocco, famiglia_slug):
    """Da un blocco `indcart_gn`: (azione, bersaglio, titolo, oggetto, pdf).

    azione: 'atto' se il blocco porta un PDF; 'pagina' se il blocco rinvia a
    una sottopagina della stessa famiglia; 'scarto' altrimenti.
    """
    pdf = None
    for l in blocco["link"]:
        href = assoluto(l["href"])
        if not href:
            continue
        if "/documents/" in href and ".pdf" in href.lower():
            pdf = href
            break
    if pdf:
        oggetto = ""
        for l in blocco["link"]:
            href = assoluto(l["href"])
            if href and ".pdf" in href.lower():
                oggetto = l["testo"]
                break
        return "atto", None, blocco["forte"], oggetto, pdf
    for l in blocco["link"]:
        href = assoluto(l["href"])
        if not href:
            continue
        if "/documents/" in href:
            continue
        if not (href.startswith(PORTALE) or href.startswith(BASE)):
            continue
        if RE_ANNO_IN_URL.search(href) and famiglia_slug and famiglia_slug not in href:
            continue
        return "pagina", href, blocco["forte"], "", None
    return "scarto", None, blocco["forte"], "", None


def testo_nodo(el):
    """Il testo di un nodo conservando i `<br/>` come a capo."""
    pezzi = []
    if el.text:
        pezzi.append(el.text)
    for figlio in el:
        if not isinstance(figlio.tag, str):
            continue
        if figlio.tag.lower() == "br":
            pezzi.append("\n")
        else:
            pezzi.append(testo_nodo(figlio))
        if figlio.tail:
            pezzi.append(figlio.tail)
    return "".join(pezzi)


def pagina(url):
    return richiesta(url).decode("utf-8", "replace")


def scorri(pagina_corpo):
    albero = LH.fromstring(pagina_corpo)
    blocchi = []
    for b in albero.xpath('//div[contains(@class,"indcart_gn")]'):
        forte = ""
        for el in b.xpath('.//strong'):
            forte = pulisci(testo_nodo(el))
            break
        link = []
        for a in b.xpath('.//a[@href]'):
            href = a.get("href") or ""
            testo = pulisci("".join(a.xpath(".//text()")))
            link.append({"href": href, "testo": testo})
        if not forte and not link:
            continue
        blocchi.append({"forte": forte, "link": link})
    return blocchi


def richiesta(url, timeout=90, tentativi=TENTATIVI):
    ultimo = None
    for tentativo in range(1, tentativi + 1):
        try:
            req = urllib.request.Request(url, headers=TESTATE)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                corpo = r.read()
                finale = r.geturl() or ""
                tipo = r.headers.get("Content-Type", "")
            if any(p in finale.lower() for p in PAROLE_ERRORE):
                raise IOError("il portale ha rimandato a una pagina d'errore")
            if "text/html" in tipo and corpo[:200].lower().find(b"<html") >= 0:
                if len(corpo) < 4000 and any(p.encode() in corpo.lower() for p in PAROLE_ERRORE):
                    raise IOError("il portale ha restituito una pagina d'errore")
            return corpo
        except urllib.error.HTTPError as e:
            ultimo = "HTTP %d" % e.code
            if e.code not in RIPROVABILI:
                raise IOError(ultimo)
        except Exception as e:
            ultimo = "%s: %s" % (type(e).__name__, e)
            if str(e).startswith("il portale"):
                raise
        if tentativo < tentativi:
            attesa = 1.5 * tentativo + (hash(url) % 7) / 10.0
            time.sleep(attesa)
    raise IOError("%s dopo %d tentativi" % (ultimo, tentativi))


# ------------------------------------------------------------- il giro



def conosciuti_da_s3(cliente, chiavi):
    """Dedup senza OpenSearch, per il giro sul runner di Milano: elenca
    l'archivio PDF (PREFISSO_PDF) e dice quali chiavi_fonte sono gia'
    archiviate. L'archivio contiene TUTTO lo storico, quindi la dedup e'
    completa; i record _rec contengono solo cio' che ha scaricato il runner."""
    if not chiavi or cliente is None:
        return set()
    per_id = {}
    for cf in chiavi:
        per_id[str(uuid.uuid5(NOMESPACE, "giustup:agenziaentrate:" + cf))] = cf
    trovati = set()
    try:
        paginatore = cliente.get_paginator("list_objects_v2")
        for pagina in paginatore.paginate(Bucket=BUCKET, Prefix=PREFISSO_PDF + "/"):
            for o in pagina.get("Contents", []):
                k = o["Key"]
                if not k.endswith(".pdf"):
                    continue
                stem = k.rsplit("/", 1)[-1][:-4]
                cf = per_id.pop(stem, None)
                if cf is not None:
                    trovati.add(cf)
    except Exception as e:
        log("dedup da S3 non disponibile (%s): si riscarica tutto" % (e,))
    return trovati


def sincronizza(args):
    prova = max(0, int(args.prova or 0))
    global _salva_record
    _salva_record = not prova and not args.senza_s3 and bool(getattr(args, "salva_record", False))
    scrivi = not prova and not args.senza_s3
    indice = not prova and not args.senza_indice
    cliente_s3 = s3() if scrivi else None

    if indice:
        crea_indice_se_manca()

    elenco_famiglie = famiglie()
    if args.categoria:
        volute = set()
        for c in args.categoria:
            volute.add(c)
            volute.add(slug(c))
        elenco_famiglie = [f for f in elenco_famiglie
                           if f[1]["categoria"] in volute
                           or f[0] in volute]
        if not elenco_famiglie:
            log("nessuna famiglia corrisponde a %s" % ", ".join(args.categoria))
            return 2
    log("famiglie: %d (%s)" % (len(elenco_famiglie),
                               ", ".join(f[0] for f in elenco_famiglie)))

    inizio = time.time()
    totale_nuovi = totale_visti = totale_falliti = 0
    per_categoria, falliti, righe_manifesto = {}, [], []
    di_fila = 0
    scaricati = 0
    lotto = []
    visti_pdf = set()

    def svuota_lotto():
        nonlocal totale_falliti
        if not lotto or not indice:
            return
        try:
            indicizza(lotto)
        except Exception as e:
            log("indicizzazione di %d documenti: %s" % (len(lotto), e))
            totale_falliti += len(lotto)
            for d in lotto:
                falliti.append({"url": d["chiave_fonte"],
                                "motivo": "indice: %s" % str(e)[:160]})
        del lotto[:]

    for famiglia_slug, voce in elenco_famiglie:
        categoria = voce["categoria"]
        voce_nome = voce["nome"]
        conto = {"visti": 0, "nuovi": 0, "falliti": 0}
        per_categoria[categoria] = conto
        da_visitare = list(voce["radici"])
        visitate = set()
        rami_fermi = 0
        nuovi_famiglia = 0

        while da_visitare:
            if prova and nuovi_famiglia >= prova:
                break
            if args.massimo and scaricati >= args.massimo:
                break
            url = da_visitare.pop(0)
            if url in visitate:
                continue
            visitate.add(url)
            try:
                corpo = pagina(url)
            except Exception as e:
                log("%s: pagina non letta (%s): %s" % (famiglia_slug, url[:70], e))
                di_fila += 1
                if di_fila >= FALLIMENTI_DI_FILA:
                    log("il portale non risponde più: il giro si ferma")
                    svuota_lotto()
                    return 2
                continue
            di_fila = 0
            if len(corpo) < 4000:
                continue

            blocchi = scorri(corpo)
            novita = 0
            nuova_pagina = False

            # Gli indirizzi dei PDF di questa pagina, per chiedere una sola
            # volta all'indice quali sono già lì e non riscaricarli.
            pdf_pagina = [pdf for b in blocchi
                          for azione, _bg, _ti, _og, pdf in
                          [valuta_blocco(b, famiglia_slug)]
                          if azione == "atto" and pdf]
            gia = set()
            if not args.tutto and pdf_pagina:
                try:
                    gia = conosciuti(pdf_pagina) if indice else conosciuti_da_s3(cliente_s3, pdf_pagina)
                except Exception as e:
                    log("indice non interrogabile (%s): si riscarica tutto" % (e,))

            for b in blocchi:
                azione, bersaglio, titolo, oggetto, pdf = valuta_blocco(b, famiglia_slug)
                if azione == "pagina" and bersaglio:
                    if bersaglio not in visitate and bersaglio not in da_visitare:
                        if re.search(r"(animated|\;jsessionid)", bersaglio):
                            continue
                        da_visitare.append(bersaglio)
                        nuova_pagina = True
                    continue
                if azione == "atto" and pdf:
                    conto["visti"] += 1
                    totale_visti += 1
                    if pdf in visti_pdf:
                        continue
                    visti_pdf.add(pdf)
                    if pdf in gia:
                        continue
                    try:
                        documento = lavora(pdf, titolo, oggetto,
                                           categoria, voce_nome, scrivi,
                                           cliente_s3)
                    except Exception as e:
                        conto["falliti"] += 1
                        totale_falliti += 1
                        falliti.append({"url": pdf, "motivo": str(e)[:200]})
                        log("    %s: %s" % (pdf[:80], e))
                        di_fila += 1
                        if di_fila >= FALLIMENTI_DI_FILA:
                            log("troppi documenti di fila non scaricati: il giro si ferma")
                            svuota_lotto()
                            return 2
                        continue
                    di_fila = 0
                    novita += 1
                    nuovi_famiglia += 1
                    scaricati += 1
                    conto["nuovi"] += 1
                    righe_manifesto.append([
                        documento["id_nodo"], documento["chiave_s3"],
                        documento["chiave_fonte"], documento["categoria"],
                        documento["numero"] or "", documento["anno"] or "",
                        documento["data"] or "", documento["oggetto"][:200],
                        documento["caratteri"], documento["impronta"],
                    ])
                    if indice:
                        lotto.append(documento)
                        if len(lotto) >= RIGHE_PER_LOTTO:
                            svuota_lotto()
                    if prova:
                        log("    [prova] %s | %s | n. %s/%s | %s | %d caratteri"
                            % (documento["categoria"], documento["titolo"][:60],
                               documento["numero"], documento["anno"],
                               documento["data"], documento["caratteri"]))
                    time.sleep(PAUSA_FRA_PDF)
                    if scaricati % PAUSA_OGNI == 0:
                        time.sleep(PAUSA_LUNGA)
                        log("  ... %d documenti in questo giro, %d falliti, %.0f s"
                            % (scaricati, totale_falliti, time.time() - inizio))
                if novita == 0 and not nuova_pagina:
                    rami_fermi += 1
                    if rami_fermi >= 3:
                        log("%s: alcuni rami senza novità, mi fermo su questo ramo"
                            % famiglia_slug)
                        break
                else:
                    rami_fermi = 0

        log("%s: %d visti, %d nuovi, %d falliti"
            % (famiglia_slug, conto["visti"], conto["nuovi"], conto["falliti"]))
        if args.massimo and scaricati >= args.massimo:
            break

    svuota_lotto()

    esito = 1 if totale_falliti else 0

    manifesto = ""
    if righe_manifesto and scrivi:
        manifesto = "manifesto-agenziaentrate-%s.csv" % adesso().strftime("%Y%m%d")
        colonne = ["id_nodo", "chiave_s3", "chiave_fonte", "categoria", "numero",
                   "anno", "data", "oggetto", "caratteri", "impronta"]
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                         encoding="utf-8-sig", newline="") as f:
            scrittore = csv.writer(f, delimiter=";")
            scrittore.writerow(colonne)
            for riga in righe_manifesto:
                scrittore.writerow(riga)
            percorso = f.name
        try:
            carica(cliente_s3, "lavorazione/manifesto/" + manifesto,
                   open(percorso, "rb").read(), "text/csv; charset=utf-8")
        except Exception as e:
            log("manifesto non caricato su S3: %r" % (e,))
            manifesto = ""
        finally:
            os.unlink(percorso)

    riassunto = {
        "ultimoGiro": adesso().isoformat().replace("+00:00", "Z"),
        "esito": esito,
        "durataSecondi": int(time.time() - inizio),
        "documentiVisti": totale_visti,
        "documentiNuovi": totale_nuovi,
        "documentiFalliti": totale_falliti,
        "falliti_elenco": falliti[:200],
        "perCategoria": per_categoria,
        "manifesto": manifesto,
        "indice": IDX,
        "versionePipeline": "agenziaentrate-1.0",
    }
    if prova:
        log("[prova] nessuna scrittura: stato non pubblicato")
    else:
        stato(riassunto)
        metrica("EsitoSyncAgenziaEntrate", esito)
        metrica("DocumentiNonScaricatiAgenziaEntrate", totale_falliti)
    log("fine: %d nuovi, %d falliti, %d secondi"
        % (totale_nuovi, totale_falliti, riassunto["durataSecondi"]))
    return esito


def slug(testo):
    t = pulisci(testo).lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    return t.strip("-") or "altro"


def stampa_famiglie():
    elenco = famiglie()
    log("famiglie Agenzia delle Entrate: %d" % len(elenco))
    for slug_, voce in elenco:
        print("%-16s %-40s %s" % (slug_, voce["categoria"], voce["nome"]))
    return 0


def indicizza_da_s3(args):
    """Il lato motore della scissione: legge da S3 i record lasciati dal giro di
    Milano (`--salva-record`) e li indica in OpenSearch. Non tocca la fonte:
    carica solo quello che l'archivio non ha ancora (confronto su `chiave_fonte`)."""
    crea_indice_se_manca()
    cliente = s3()
    chiavi = []
    paginatore = cliente.get_paginator("list_objects_v2")
    try:
        for pagina in paginatore.paginate(Bucket=BUCKET, Prefix=PREFISSO_REC + "/"):
            for o in pagina.get("Contents", []):
                if o["Key"].endswith(".json"):
                    chiavi.append(o["Key"])
    except Exception as e:
        log("elenco record su S3 non leggibile: %r" % (e,))
        return 2

    documenti = []
    for k in chiavi:
        try:
            corpo = cliente.get_object(Bucket=BUCKET, Key=k)["Body"].read()
            documenti.append(json.loads(corpo.decode("utf-8")))
        except Exception as e:
            log("record %s non letto: %r" % (k, e))

    fonti = [d.get("chiave_fonte") for d in documenti if d.get("chiave_fonte")]
    gia = set()
    if fonti:
        try:
            gia = conosciuti(fonti)
        except Exception as e:
            log("indice non interrogabile (%s): si ricaricano tutti" % (e,))
    nuovi = [d for d in documenti if d.get("chiave_fonte") not in gia]
    log("record su S3: %d, gia' in indice: %d, da indicizzare: %d"
        % (len(documenti), len(documenti) - len(nuovi), len(nuovi)))

    inizio = time.time()
    falliti = 0
    for i in range(0, len(nuovi), 25):
        lotto = nuovi[i:i + 25]
        try:
            indicizza(lotto)
        except Exception as e:
            falliti += len(lotto)
            log("indicizzazione di %d documenti: %s" % (len(lotto), e))

    riassunto = {
        "ultimoGiro": adesso().isoformat().replace("+00:00", "Z"),
        "esito": 1 if falliti else 0,
        "durataSecondi": int(time.time() - inizio),
        "documentiSuS3": len(documenti),
        "documentiIndicizzati": len(nuovi) - falliti,
        "documentiFalliti": falliti,
        "indice": IDX,
        "versionePipeline": "agenziaentrate-1.0",
    }
    stato(riassunto)
    metrica("EsitoIndicizzaAgenziaEntrate", riassunto["esito"])
    log("indicizza-da-s3: %d su %d, %d falliti, %d s"
        % (riassunto["documentiIndicizzati"], riassunto["documentiSuS3"],
           falliti, riassunto["durataSecondi"]))
    return riassunto["esito"]


def main():
    a = argparse.ArgumentParser(description="Sync della prassi Agenzia delle Entrate per Giustup")
    a.add_argument("--prova", type=int, default=0,
                   help="quanti atti per famiglia, senza scrivere")
    a.add_argument("--categoria", action="append",
                   help="una famiglia (slug o categoria); ripetibile")
    a.add_argument("--tutto", action="store_true",
                   help="recupera anche quello che c'è già")
    a.add_argument("--massimo", type=int, default=0,
                   help="tetto di documenti in questo giro")
    a.add_argument("--anno", type=int, default=0,
                   help="solo un anno (filtro, non accelera)")
    a.add_argument("--senza-indice", action="store_true",
                   help="non tocca OpenSearch")
    a.add_argument("--senza-s3", action="store_true",
                   help="non carica su S3")
    a.add_argument("--salva-record", action="store_true",
                   help="(scissione Milano) salva anche il record JSON su S3 per il motore")
    a.add_argument("--indicizza-da-s3", action="store_true",
                   help="(scissione motore) indicizza in OpenSearch i record lasciati su S3 da Milano")
    a.add_argument("--famiglie", action="store_true",
                   help="stampa le famiglie ed esce")
    args = a.parse_args()

    if args.indicizza_da_s3:
        return indicizza_da_s3(args)

    if args.famiglie:
        return stampa_famiglie()

    try:
        return sincronizza(args)
    except KeyboardInterrupt:
        log("interrotto")
        return 2
    except Exception as e:
        log("errore: %r" % (e,))
        return 2


if __name__ == "__main__":
    sys.exit(main())