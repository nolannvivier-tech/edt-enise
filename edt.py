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
    colonnes_jours = []
    candidats_entetes = page.locator("th:visible, div:visible, span:visible, td:visible").filter(
        has_text=re.compile(r"\b(lun|mar|mer|jeu|ven|sam)[a-z]*\.?\b", re.IGNORECASE)
    ).all()

    for entete in candidats_entetes:
        txt = entete.inner_text().strip()
        if len(txt) <= 30 and not re.search(r"\d{1,2}[h:]\d{2}", txt):
            date_col = extraire_date_depuis_texte(txt, maintenant)
            box = entete.bounding_box()
            if date_col and box and box["width"] > 30:
                colonnes_jours.append({
                    "x_centre": box["x"] + box["width"] / 2,
                    "date": date_col,
                    "label": txt,
                })
    colonnes_jours.sort(key=lambda c: c["x_centre"])
    return colonnes_jours


def signature_semaine(colonnes_jours: list[dict]) -> str:
    return "|".join(c["label"] for c in colonnes_jours)


def attendre_changement_semaine(page, signature_avant: str, maintenant: datetime, timeout_ms: int = 1800) -> bool:
    pas_ms = 150
    for _ in range(timeout_ms // pas_ms):
        page.wait_for_timeout(pas_ms)
        cols = reperer_colonnes_jours(page, maintenant)
        sig = signature_semaine(cols)
        if sig and sig != signature_avant:
            page.wait_for_timeout(250)
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

    selecteurs_possibles = [
        ".EmploiDuTemps_Element",
        ".fc-event",
        ".event",
        ".cours",
        "[class*='event']",
        "[class*='cours']",
        "[class*='slot']",
    ]

    evenements = []

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
                r"(?:de\s+)?(\d{1,2})[h:](\d{2})\s*(?:-|–|—|à|/)\s*(\d{1,2})[h:](\d{2})",
                source_complete,
            )
            if not m_heure:
                continue

            h1, min1, h2, min2 = map(int, m_heure.groups())

            date_cours = None
            if colonnes_jours:
                box = el.bounding_box()
                if box:
                    x_cours = box["x"] + box["width"] / 2
                    col_proche = min(colonnes_jours, key=lambda c: abs(c["x_centre"] - x_cours))
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

        if evenements:
            break

    return evenements, sig


def trouver_barre_semaines(page) -> tuple[list[dict], float | None]:
    """Repère la ligne horizontale unique contenant les pastilles de semaines (1..53)."""
    candidats = page.locator("div:visible, td:visible, span:visible, li:visible, a:visible, button:visible").all()
    items = []
    for el in candidats:
        try:
            txt = el.inner_text().strip()
            if not re.fullmatch(r"\d{1,2}", txt):
                continue
            num = int(txt)
            if not (1 <= num <= 53):
                continue
            box = el.bounding_box()
            if not box or box["y"] > 300 or box["width"] > 80 or box["height"] > 60:
                continue
            items.append({
                "el": el,
                "num": num,
                "x": box["x"] + box["width"] / 2,
                "y": box["y"] + box["height"] / 2,
            })
        except Exception:
            continue

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
    candidats = page.locator("div:visible, td:visible, span:visible, button:visible, i:visible, a:visible").all()
    meilleur = None
    for el in candidats:
        try:
            box = el.bounding_box()
            if not box or box["width"] > 60 or box["height"] > 60:
                continue
            yc = box["y"] + box["height"] / 2
            xc = box["x"] + box["width"] / 2
            if abs(yc - y_barre) > 15:
                continue
            if cote == "gauche" and xc < x_limite - 5:
                if meilleur is None or xc > meilleur[0]:
                    meilleur = (xc, el)
            elif cote == "droite" and xc > x_limite + 5:
                if meilleur is None or xc < meilleur[0]:
                    meilleur = (xc, el)
        except Exception:
            continue

    if meilleur:
        try:
            meilleur[1].click(timeout=1500)
            page.wait_for_timeout(400)
            return True
        except Exception:
            return False
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
                # Défile tout à gauche jusqu'à septembre puis sélectionne la première semaine
                for _ in range(10):
                    premier_avant = bande[0]["num"]
                    if not cliquer_fleche_barre(page, y_barre, "gauche", bande[0]["x"]):
                        break
                    bande, y_barre = trouver_barre_semaines(page)
                    if not bande or bande[0]["num"] == premier_avant:
                        break

                sig_avant = signature_semaine(reperer_colonnes_jours(page, maintenant))
                bande[0]["el"].click(timeout=2000)
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
                prochaine["el"].click(timeout=2000)
                attendre_changement_semaine(page, sig, maintenant)
        else:
            # Repli de sécurité : extrait au minimum la semaine courante affichée
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
