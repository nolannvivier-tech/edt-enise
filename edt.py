import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

URL_EDT = "https://edt.enise.fr/invite"
NOM_ETUDIANT = "VIVIER"  # Passe à "VIVIER Nolann" si plusieurs homonymes apparaissent
HEADLESS = True
DEPUIS_DEBUT_ANNEE = True  # True = de septembre à juillet | False = d'aujourd'hui à juillet


MOIS_FR = {
    "janv": 1, "jan": 1,
    "févr": 2, "fevr": 2, "fév": 2, "fev": 2,
    "mars": 3, "mar": 3,
    "avr": 4,
    "mai": 5,
    "juin": 6,
    "juil": 7,
    "août": 8, "aout": 8,
    "sept": 9, "sep": 9,
    "oct": 10,
    "nov": 11,
    "déc": 12, "dec": 12,
}

JOURS_FR = {
    "lun": 0,
    "mar": 1,
    "mer": 2,
    "jeu": 3,
    "ven": 4,
    "sam": 5,
    "dim": 6,
}


def paris_vers_utc(dt_local: datetime) -> datetime:
    """Convertit une heure locale française en UTC (gestion heure d'été/hiver)."""
    annee = dt_local.year
    mar31 = datetime(annee, 3, 31)
    debut_ete = mar31 - timedelta(days=(mar31.weekday() + 1) % 7) + timedelta(hours=2)
    oct31 = datetime(annee, 10, 31)
    fin_ete = oct31 - timedelta(days=(oct31.weekday() + 1) % 7) + timedelta(hours=3)
    decalage = timedelta(hours=2) if debut_ete <= dt_local < fin_ete else timedelta(hours=1)
    return dt_local - decalage


def echapper_ics(texte: str) -> str:
    return (
        texte.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def generer_uid_fixe(dtstart: datetime, dtend: datetime, summary: str) -> str:
    """Produit un UID stable pour qu'un cours modifié soit mis à jour sans doublon."""
    cle = f"{dtstart.isoformat()}|{dtend.isoformat()}|{summary}".encode("utf-8")
    empreinte = hashlib.sha256(cle).hexdigest()[:24]
    return f"{empreinte}@edt.enise.fr"


def generer_ics(evenements: list[dict], chemin_sortie: str = "edt_enise.ics") -> int:
    lignes = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ENISE EDT Sync//FR",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:EDT ENISE",
    ]

    maintenant_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    nb_ajoutes = 0

    for ev in evenements:
        if not ev.get("dtstart") or not ev.get("dtend"):
            continue

        dtstart_utc = paris_vers_utc(ev["dtstart"]).strftime("%Y%m%dT%H%M%SZ")
        dtend_utc = paris_vers_utc(ev["dtend"]).strftime("%Y%m%dT%H%M%SZ")
        uid = generer_uid_fixe(ev["dtstart"], ev["dtend"], ev["summary"])

        lignes.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{maintenant_utc}",
            f"DTSTART:{dtstart_utc}",
            f"DTEND:{dtend_utc}",
            f"SUMMARY:{echapper_ics(ev['summary'])}",
        ])
        if ev.get("location"):
            lignes.append(f"LOCATION:{echapper_ics(ev['location'])}")
        if ev.get("description"):
            lignes.append(f"DESCRIPTION:{echapper_ics(ev['description'])}")
        lignes.append("END:VEVENT")
        nb_ajoutes += 1

    lignes.append("END:VCALENDAR")

    with open(chemin_sortie, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lignes) + "\r\n")

    return nb_ajoutes


def annee_pour_mois(mois: int, maintenant: datetime) -> int:
    """Déduit l'année civile exacte d'un mois au sein de l'année universitaire (août -> juillet)."""
    annee_rentree = maintenant.year if maintenant.month >= 8 else maintenant.year - 1
    return annee_rentree if mois >= 8 else annee_rentree + 1


def extraire_date_depuis_texte(texte: str, maintenant: datetime) -> datetime | None:
    texte_propre = re.sub(r"\(\s*\d+\s*/\s*\d+\s*\)", "", texte)

    m_fr = re.search(r"\b(\d{1,2})\s+([a-zàâäéèêëîïôöùûüç]{3,10})\.?\b", texte_propre, re.IGNORECASE)
    if m_fr:
        jour = int(m_fr.group(1))
        mois_txt = m_fr.group(2).lower()
        mois = MOIS_FR.get(mois_txt[:4]) or MOIS_FR.get(mois_txt[:3])
        if mois:
            try:
                return datetime(annee_pour_mois(mois, maintenant), mois, jour)
            except ValueError:
                pass

    m_num = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", texte_propre)
    if m_num:
        jour, mois = int(m_num.group(1)), int(m_num.group(2))
        if m_num.group(3):
            annee = int(m_num.group(3))
            if annee < 100:
                annee += 2000
        else:
            annee = annee_pour_mois(mois, maintenant)
        try:
            return datetime(annee, mois, jour)
        except ValueError:
            pass

    m_jour = re.search(r"\b(lun|mar|mer|jeu|ven|sam|dim)[a-z]*\.?\b", texte_propre, re.IGNORECASE)
    if m_jour:
        idx_jour = JOURS_FR[m_jour.group(1).lower()]
        lundi_semaine = datetime(maintenant.year, maintenant.month, maintenant.day) - timedelta(
            days=maintenant.weekday()
        )
        return lundi_semaine + timedelta(days=idx_jour)

    return None


def reperer_colonnes_jours(page, maintenant: datetime) -> list[dict]:
    """Capture atomiquement les en-têtes des jours pour éviter toute erreur si le DOM se rafraîchit."""
    try:
        bruts = page.evaluate(r"""() => {
            const res = [];
            const regex = /\b(lun|mar|mer|jeu|ven|sam)[a-z]*\.?\b/i;
            const els = document.querySelectorAll('th, div, span, td');
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 30 || rect.height <= 0) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const txt = (el.innerText || '').trim();
                if (txt.length > 0 && txt.length <= 30 && regex.test(txt) && !/\d{1,2}[h:]\d{2}/.test(txt)) {
                    res.push({
                        txt: txt,
                        x_centre: rect.x + rect.width / 2
                    });
                }
            }
            return res;
        }""")
    except Exception:
        return []

    colonnes_jours = []
    deja_vus = set()
    for item in bruts:
        date_col = extraire_date_depuis_texte(item["txt"], maintenant)
        if date_col:
            cle = (date_col, round(item["x_centre"] / 15))
            if cle not in deja_vus:
                deja_vus.add(cle)
                colonnes_jours.append({
                    "x_centre": item["x_centre"],
                    "date": date_col,
                    "label": item["txt"],
                })
    colonnes_jours.sort(key=lambda c: c["x_centre"])
    return colonnes_jours


def signature_semaine(colonnes_jours: list[dict]) -> str:
    return "|".join(c["label"] for c in colonnes_jours)


def attendre_changement_semaine(page, signature_avant: str, maintenant: datetime, timeout_ms: int = 3500) -> bool:
    pas_ms = 150
    for _ in range(timeout_ms // pas_ms):
        page.wait_for_timeout(pas_ms)
        cols = reperer_colonnes_jours(page, maintenant)
        sig = signature_semaine(cols)
        if sig and sig != signature_avant:
            page.wait_for_timeout(300)
            return True
    return False


def formater_lieu(infos_cours: list[str]) -> str:
    salles = []
    adresse = ""

    for ligne in infos_cours[1:]:
        if re.search(r"\b(rue|avenue|boulevard|allée|place|impasse|jean parot)\b", ligne, re.I):
            adresse = ligne
            continue
        if re.match(r"^\d+[A-Z]{2,}-", ligne, re.I) or re.match(r"^(CM|TD|TP|CMTD|DS)\b", ligne, re.I):
            continue
        if re.match(
            r"^(?:salle\s+|amphi\s+|labo\s+|atelier\s+|gymnase\s+|halle\s+)?(?:\d{2,4}(?:\s+[A-Z0-9-]+)?|[A-Z]\d{2,3}|amphi\s*\w+|gymnase\w*|halle\w*)$",
            ligne,
            re.I,
        ):
            salles.append(ligne)

    texte_salle = ""
    if salles:
        prefixe = "Salle " if re.match(r"^\d", salles[0]) else ""
        texte_salle = f"{prefixe}{', '.join(salles)}"

    return " - ".join(part for part in (texte_salle, adresse) if part)


def extraire_cours_visibles(page, maintenant: datetime) -> tuple[list[dict], str]:
    colonnes_jours = reperer_colonnes_jours(page, maintenant)
    sig = signature_semaine(colonnes_jours)

    try:
        donnees_cours = page.evaluate(r"""() => {
            const selecteurs = [
                '.EmploiDuTemps_Element',
                '.fc-event',
                '.event',
                '.cours',
                "[class*='event']",
                "[class*='cours']",
                "[class*='slot']"
            ];
            const timeRegex = /(?:de\s+)?\d{1,2}[h:]\d{2}\s*(?:-|–|—|à|\/)\s*\d{1,2}[h:]\d{2}/;
            for (const sel of selecteurs) {
                const els = Array.from(document.querySelectorAll(sel));
                if (els.length === 0) continue;
                const items = [];
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const texte = (el.innerText || '').trim();
                    if (!texte) continue;
                    const aria = el.getAttribute('aria-label') || el.getAttribute('title') || '';
                    const sourceComplete = `${aria} | ${texte}`;
                    if (!timeRegex.test(sourceComplete)) continue;
                    items.push({
                        texte: texte,
                        aria: aria,
                        x_centre: rect.x + rect.width / 2
                    });
                }
                if (items.length > 0) return items;
            }
            return [];
        }""")
    except Exception:
        return [], sig

    evenements = []

    for item in donnees_cours:
        texte = item["texte"]
        aria = item["aria"]
        source_complete = f"{aria} | {texte}".strip(" |")

        m_heure = re.search(
            r"(?:de\s+)?(\d{1,2})[h:](\d{2})\s*(?:-|–|—|à|/)\s*(\d{1,2})[h:](\d{2})",
            source_complete,
        )
        if not m_heure:
            continue

        h1, min1, h2, min2 = map(int, m_heure.groups())

        date_cours = None
        if colonnes_jours:
            col_proche = min(colonnes_jours, key=lambda c: abs(c["x_centre"] - item["x_centre"]))
            date_cours = col_proche["date"]

        if not date_cours:
            date_cours = extraire_date_depuis_texte(aria or texte, maintenant)

        if not date_cours:
            continue

        lignes_texte = [l.strip() for l in texte.splitlines() if l.strip()]
        infos_cours = [
            l for l in lignes_texte
            if not re.search(r"^\d{1,2}[h:]\d{2}\s*(?:-|–|—|à)\s*\d{1,2}[h:]\d{2}$", l)
        ]
        titre = infos_cours[0] if infos_cours else "Cours ENISE"
        lieu = formater_lieu(infos_cours)

        evenements.append({
            "dtstart": date_cours.replace(hour=h1, minute=min1),
            "dtend": date_cours.replace(hour=h2, minute=min2),
            "summary": titre,
            "location": lieu,
            "description": " | ".join(lignes_texte),
        })

    return evenements, sig


def trouver_barre_semaines(page) -> tuple[list[dict], float | None]:
    """Repère la ligne horizontale unique contenant les pastilles de semaines (1..53)."""
    try:
        items = page.evaluate(r"""() => {
            const res = [];
            const els = document.querySelectorAll('div, td, span, li, a, button');
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || rect.y > 300 || rect.width > 80 || rect.height > 60) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const txt = (el.innerText || '').trim();
                if (/^\d{1,2}$/.test(txt)) {
                    const num = parseInt(txt, 10);
                    if (num >= 1 && num <= 53) {
                        res.push({
                            num: num,
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2
                        });
                    }
                }
            }
            return res;
        }""")
    except Exception:
        return [], None

    if not items:
        return [], None

    meilleure_bande = []
    for ref in items:
        bande = [it for it in items if abs(it["y"] - ref["y"]) <= 10]
        bande.sort(key=lambda it: it["x"])
        bande_unique = []
        for it in bande:
            if not bande_unique or abs(it["x"] - bande_unique[-1]["x"]) > 8:
                bande_unique.append(it)
        if len(bande_unique) > len(meilleure_bande):
            meilleure_bande = bande_unique

    if len(meilleure_bande) < 5:
        return [], None

    y_moyen = sum(it["y"] for it in meilleure_bande) / len(meilleure_bande)
    return meilleure_bande, y_moyen


def cliquer_fleche_barre(page, y_barre: float, cote: str, x_limite: float) -> bool:
    """Clique sur la flèche de défilement gauche ou droite située sur l'axe Y de la barre des semaines."""
    try:
        cible = page.evaluate(r"""({ yBarre, cote, xLimite }) => {
            const els = document.querySelectorAll('div, td, span, button, i, a');
            let meilleur = null;
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0 || rect.width > 60 || rect.height > 60) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                const yc = rect.y + rect.height / 2;
                const xc = rect.x + rect.width / 2;
                if (Math.abs(yc - yBarre) > 15) continue;
                if (cote === 'gauche' && xc < xLimite - 5) {
                    if (!meilleur || xc > meilleur.x) meilleur = { x: xc, y: yc };
                } else if (cote === 'droite' && xc > xLimite + 5) {
                    if (!meilleur || xc < meilleur.x) meilleur = { x: xc, y: yc };
                }
            }
            return meilleur;
        }""", {"yBarre": y_barre, "cote": cote, "xLimite": x_limite})
    except Exception:
        return False

    if cible:
        page.mouse.click(cible["x"], cible["y"])
        page.wait_for_timeout(400)
        return True
    return False


def recuperer_edt():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = context.new_page()

        print(f"Connexion à {URL_EDT}...")
        page.goto(URL_EDT, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        btn_etudiant = page.locator(
            "a:visible, button:visible, li:visible, div[role='tab']:visible, span:visible, label:visible"
        ).filter(has_text=re.compile(r"^\s*[ée]tudiants?\s*$", re.IGNORECASE)).first

        if btn_etudiant.count() == 0:
            btn_etudiant = page.get_by_text(re.compile(r"[ée]tudiants?", re.IGNORECASE)).first

        btn_etudiant.wait_for(state="visible", timeout=15000)
        btn_etudiant.click()
        page.wait_for_timeout(1500)

        search_input = page.locator("input[type='text']:visible, input[type='search']:visible").first
        search_input.wait_for(state="visible", timeout=15000)

        search_input.click()
        search_input.fill(NOM_ETUDIANT)
        page.wait_for_timeout(2000)

        suggestion = page.locator(
            ".ui-menu-item:visible, .autocomplete-suggestion:visible, li[role='option']:visible, .as-li:visible"
        ).first
        if suggestion.count() > 0:
            suggestion.click()
        else:
            search_input.press("Enter")

        page.wait_for_timeout(3500)
        maintenant = datetime.now()

        evenements_uniques = []
        cles_vues = set()
        semaines_traitees = set()

        bande, y_barre = trouver_barre_semaines(page)
        print(f"Barre des semaines détectée : {[b['num'] for b in bande]}")

        if bande and y_barre is not None:
            if DEPUIS_DEBUT_ANNEE:
                for _ in range(10):
                    premier_avant = bande[0]["num"]
                    if not cliquer_fleche_barre(page, y_barre, "gauche", bande[0]["x"]):
                        break
                    bande, y_barre = trouver_barre_semaines(page)
                    if not bande or bande[0]["num"] == premier_avant:
                        break

                sig_avant = signature_semaine(reperer_colonnes_jours(page, maintenant))
                page.mouse.click(bande[0]["x"], bande[0]["y"])
                attendre_changement_semaine(page, sig_avant, maintenant)
                semaines_traitees.add(bande[0]["num"])
            else:
                sem_actuelle = maintenant.isocalendar().week
                for b in bande:
                    if b["num"] == sem_actuelle:
                        break
                    semaines_traitees.add(b["num"])
                semaines_traitees.add(sem_actuelle)

            for _ in range(52):
                cours_semaine, sig = extraire_cours_visibles(page, maintenant)
                ajoutes = 0
                for ev in cours_semaine:
                    cle = (ev["dtstart"], ev["dtend"], ev["summary"])
                    if cle not in cles_vues:
                        cles_vues.add(cle)
                        evenements_uniques.append(ev)
                        ajoutes += 1

                lundi_txt = sig.split("|")[0] if sig else "?"
                print(f"Semaine ({lundi_txt}) : {ajoutes} cours.")

                bande, y_barre = trouver_barre_semaines(page)
                if not bande or y_barre is None:
                    break

                prochaine = next((b for b in bande if b["num"] not in semaines_traitees), None)
                if prochaine is None:
                    if cliquer_fleche_barre(page, y_barre, "droite", bande[-1]["x"]):
                        bande, y_barre = trouver_barre_semaines(page)
                        prochaine = next((b for b in bande if b["num"] not in semaines_traitees), None)

                if prochaine is None:
                    print("Fin de la barre des semaines atteinte.")
                    break

                semaines_traitees.add(prochaine["num"])
                page.mouse.click(prochaine["x"], prochaine["y"])
                attendre_changement_semaine(page, sig, maintenant)
        else:
            cours_semaine, _ = extraire_cours_visibles(page, maintenant)
            evenements_uniques.extend(cours_semaine)

        browser.close()

        if not evenements_uniques:
            print("Aucun événement extrait, fichier existant conservé.", file=sys.stderr)
            sys.exit(1)

        nb_ics = generer_ics(evenements_uniques, "edt_enise.ics")
        print(f"Total : {nb_ics} événements exportés dans 'edt_enise.ics'.")


if __name__ == "__main__":
    recuperer_edt()
