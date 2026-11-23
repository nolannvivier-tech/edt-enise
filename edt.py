import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from playwright.sync_api import sync_playwright

URL_EDT = "https://edt.enise.fr/invite"
NOM_ETUDIANT = "VIVIER"  # Passe à "VIVIER Nolann" si plusieurs homonymes apparaissent
HEADLESS = True


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


def extraire_date_depuis_texte(texte: str, annee_defaut: int) -> datetime | None:
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", texte)
    if not m:
        return None
    jour, mois = int(m.group(1)), int(m.group(2))
    annee = int(m.group(3)) if m.group(3) else annee_defaut
    if annee < 100:
        annee += 2000
    try:
        return datetime(annee, mois, jour)
    except ValueError:
        return None


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

        page.wait_for_timeout(4000)
        annee_courante = datetime.now().year

        colonnes_jours = []
        candidats_entetes = page.locator("th:visible, div:visible, span:visible").filter(
            has_text=re.compile(r"(lun|mar|mer|jeu|ven|sam)[^\d]*\d{1,2}[/.-]\d{1,2}", re.IGNORECASE)
        ).all()

        for entete in candidats_entetes:
            txt = entete.inner_text().strip()
            if len(txt) <= 30:
                date_col = extraire_date_depuis_texte(txt, annee_courante)
                box = entete.bounding_box()
                if date_col and box:
                    colonnes_jours.append({"x_centre": box["x"] + box["width"] / 2, "date": date_col})

        selecteurs_possibles = [
            ".EmploiDuTemps_Element",
            ".fc-event",
            ".event",
            ".cours",
            "[class*='event']",
            "[class*='cours']",
            "[class*='slot']",
        ]

        evenements_ics = []

        for sel in selecteurs_possibles:
            elements = page.locator(sel).all()
            if not elements:
                continue

            for el in elements:
                texte = el.inner_text().strip()
                aria = el.get_attribute("aria-label") or el.get_attribute("title") or ""
                source_complete = f"{aria} | {texte}".strip(" |")
                if not texte:
                    continue

                m_heure = re.search(
                    r"(?:de\s+)?(\d{1,2})[h:](\d{2})\s*(?:-|–|—|à)\s*(\d{1,2})[h:](\d{2})",
                    source_complete,
                )
                if not m_heure:
                    continue

                h1, min1, h2, min2 = map(int, m_heure.groups())

                date_cours = extraire_date_depuis_texte(source_complete, annee_courante)
                if not date_cours and colonnes_jours:
                    box = el.bounding_box()
                    if box:
                        x_cours = box["x"] + box["width"] / 2
                        col_proche = min(colonnes_jours, key=lambda c: abs(c["x_centre"] - x_cours))
                        date_cours = col_proche["date"]

                if not date_cours:
                    continue

                lignes_texte = [l.strip() for l in texte.splitlines() if l.strip()]
                infos_cours = [
                    l for l in lignes_texte
                    if not re.search(r"^\d{1,2}[h:]\d{2}\s*(?:-|–|—|à)\s*\d{1,2}[h:]\d{2}$", l)
                ]
                titre = infos_cours[0] if infos_cours else "Cours ENISE"
                salle = next((l for l in infos_cours[1:] if re.search(r"salle|amphi|labo|^[A-Z]\d+", l, re.I)), "")

                evenements_ics.append({
                    "dtstart": date_cours.replace(hour=h1, minute=min1),
                    "dtend": date_cours.replace(hour=h2, minute=min2),
                    "summary": titre,
                    "location": salle,
                    "description": " | ".join(lignes_texte),
                })

            if evenements_ics:
                break

        browser.close()

        if not evenements_ics:
            print("Aucun événement extrait, fichier existant conservé.", file=sys.stderr)
            sys.exit(1)

        nb_ics = generer_ics(evenements_ics, "edt_enise.ics")
        print(f"{nb_ics} événements exportés dans 'edt_enise.ics'.")


if __name__ == "__main__":
    recuperer_edt()