# giustup-sync-agenziaentrate

Il raccoglitore che porta in giustup.it tutta la prassi dell'**Agenzia delle
Entrate** pubblicata nella sezione «Normativa e prassi»:

- **Circolari** — `normativa-e-prassi/circolari` + archivio annuale;
- **Risoluzioni** (le «decisioni») — `normativa-e-prassi/risoluzioni` + archivio;
- **Provvedimenti del Direttore** soggetti a pubblicità legale (le
  «deliberazioni») — `normativa-e-prassi/provvedimenti`;
- **Risposte alle istanze di interpello e consulenza giuridica** (i «pareri»)
  — `normativa-e-prassi/risposte-agli-interpelli`.

Fonte unica: il portale ufficiale `www.agenziaentrate.gov.it`. Ogni atto si
scarica nel suo PDF ufficiale, si conserva su S3 come prova e si indicizza nel
motore (indice `giustup-agenziaentrate`). Il PDF non si riscrive mai.

## Come lavora

Il portale organizza ogni famiglia in una gerarchia di pagine che si legge
senza JavaScript: la sezione di arrivo elenca le annate, l'annata elenca i
mesi, il mese contiene gli atti. Ogni riga della gerarchia è un blocco
`indcart_gn`: chi porta un PDF è un atto, chi porta un indirizzo `/portale/...`
è il rinvio a una pagina più in basso. Il sync cammina questa gerarchia da una
lista di radici per famiglia e segue i rami finché trova i PDF.

Gli atti già nell'indice non si riscaricano: prima di scaricare una pagina se
ne interroggano gli indirizzi PDF e si tengono solo i nuovi. Rilanciare il sync
aggiorna lo stesso documento invece di duplicarlo, perché l'identificativo
(`_id`) è un UUIDv5 dell'indirizzo del PDF.

## Uso

```bash
python3 agenziaentrate.py --famiglie              # elenca le famiglie ed esce
python3 agenziaentrate.py --prova 2               # due atti per famiglia, non scrive
python3 agenziaentrate.py --massimo 900           # il giro della notte
python3 agenziaentrate.py --categoria circolari --prova 2
python3 agenziaentrate.py --tutto --massimo 30000 # il recupero dello storico
python3 agenziaentrate.py --anno 2024 --massimo 2000   # solo un'annata
```

Codici di uscita: `0` tutto fatto; `1` qualche documento non è arrivato (resta
nello stato, si riprende la notte dopo); `2` il portale non rispondeva più.

## Scheda minima

| Voce | valore |
|---|---|
| Nome e fonte | prassi Agenzia delle Entrate, da `agenziaentrate.gov.it` |
| Autenticazione | nessuna (portale pubblico) |
| Cadenza | ogni notte alle 02:40, timer systemd `giustup-agenziaentrate.timer` |
| Tetto per giro | 900 atti (`--massimo`); al tetto il giro si ferma pulito |
| Pausa | 0,5 s fra le pagine, 1,0 s fra i PDF, 2 s ogni 20 atti |
| Identificativo | UUIDv5 dell'indirizzo del PDF (`chiave_fonte`) |
| Manifesto | `lavorazione/manifesto/manifesto-agenziaentrate-*.csv` (10 colonne) |
| Byte | `contentstore/agenziaentrate/<anno>/<id>.pdf` su S3 |
| Stato | `stato-sync/agenziaentrate.json` su S3 |
| Metriche | `EsitoSyncAgenziaEntrate`, `DocumentiNonScaricatiAgenziaEntrate` |

## Dipendenze

Python 3.9+ con `boto3` e `lxml`, e `pdftotext` (poppler-utils) per estrarre il
testo dai PDF. Si può indicare un motore o un bucket diversi con le variabili
`GIUSTUP_OPENSEARCH` e `GIUSTUP_BUCKET`.

## Deploy

```bash
# sul server, dopo il git pull
sudo cp giustup-agenziaentrate.service giustup-agenziaentrate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now giustup-agenziaentrate.timer
python3 agenziaentrate.py --prova 2   # la prova a vuoto sul motore
```