"""
repartition_gardes.py
======================

Répartition équitable des gardes et astreintes médicales sur une année,
en tenant compte des congés déclarés et des quotas individuels de
chaque médecin, avec une mémoire inter-années pour les jours de Noël
et du Nouvel An.

Refonte propre du notebook original (calendrier_2026.ipynb) : même
logique métier, même comportement, réorganisée en fonctions claires.
Aucun changement de comportement n'a été fait sauf les points listés
ci-dessous (validés explicitement avant ce nettoyage).

CHANGELOG par rapport au notebook original
--------------------------------------------
1. `choix_med` : suppression du `.format(jour)` mort sur la chaîne
   "quota_acbl_astreinte_j" (n'avait aucun effet, résidu d'une
   ancienne version).
2. `faire_don_astreinte` (ex-partie de `reprise_garde`) : l'astreinte
   n'était pas prise en charge par `reprise_garde` (seules les gardes
   vendredi/samedi/veille de férié l'étaient). Ajout d'une fonction
   dédiée qui transfère l'astreinte sur l'ensemble du week-end
   concerné (et pas seulement une date isolée, ce qui n'aurait pas de
   sens pour une astreinte qui couvre plusieurs jours).
3. `faire_don_garde` (ex `reprise_garde`) : les conditions
   `if df_calendar.loc[...].values[0] in ["vendredi", "veille_ferie"]`
   comparaient une valeur numérique (0/1) à des chaînes de caractères
   -> toujours fausses, donc sans effet (mortes). Elles sont
   supprimées : la mise à jour des totaux pondérés se fait
   maintenant uniquement via `recalculer_ponderations`, qui recalcule
   tout proprement à partir des compteurs bruts (comme le faisait
   déjà, de façon redondante, le code original juste après).
   Ajout de "dimanche" dans les compteurs bruts mis à jour, par
   souci de symétrie avec les autres jours (cette ligne n'entre
   toutefois dans aucune formule de pondération, comme dans
   l'original).
4. `suggerer_dons` (ex `recherche_compatibilite`) : ne fait plus de
   `print()`, retourne une liste de suggestions structurées. Ajoute
   une vérification explicite des congés déclarés (`med_absent`) du
   médecin à qui on suggère de donner une garde, en plus de la
   fenêtre d'immunité déjà vérifiée dans l'original.
5. Toutes les fonctions reçoivent maintenant leurs données en
   paramètres explicites (df_calendar, df_medecin, annee...) plutôt
   que de dépendre de variables globales du notebook.
6. `med_absent` : `reset_index(drop=True)` au lieu de
   `reset_index().drop("Index", axis=1)`, pour ne plus dépendre du
   nom exact de la colonne d'index du fichier Excel source.

Ce qui n'a PAS changé (comportement volontairement préservé)
--------------------------------------------------------------
- La garde du jour férié lui-même (25/12, 1er janvier) est choisie
  sur un compte BRUT (non pondéré par quota) : c'est le mécanisme de
  mémoire inter-années (`df_fetes`) qui assure l'équité sur ces
  jours, pas la pondération par quota.
- La pondération de l'astreinte (x2, +1 si jour férié accolé) reflète
  le nombre de jours réellement couverts (2 pour un week-end normal,
  3 pour un week-end avec férié accolé).
"""

import ast
import random
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from workalendar.europe import France


# ======================================================================
# 1. Génération du calendrier de campagne
# ======================================================================

def generer_calendrier(date_debut_campagne, date_fin_campagne) -> tuple:
    """Construit le calendrier jour par jour entre les bornes de la campagne
    (`campagne_config.date_debut_campagne` / `date_fin_campagne`, bornes incluses).

    Qualifie chaque jour (vendredi/samedi/dimanche, veille de férié,
    férié, veille de lundi férié, lundi férié), numérote les week-ends
    (WE_N) en fusionnant les fériés adjacents au même numéro, et
    qualifie chaque numéro de week-end en "double" (2 jours) ou
    "triple" (3 jours, férié accolé).

    Retourne (df_calendar, annee) où `annee` est déduite de la date de
    début de campagne et sert de référence pour les gardes de Noël
    (25/12/annee) et du Nouvel An (01/01/annee+1) : les bornes doivent
    donc couvrir ces deux dates.
    """
    cal = France()

    start = pd.Timestamp(date_debut_campagne)
    end = pd.Timestamp(date_fin_campagne)
    annee = start.year
    dates = [start + timedelta(days=x) for x in range((end - start).days + 1)]
    dates_str = [d.strftime("%m-%d-%Y") for d in dates]

    if "12-25-{0}".format(annee) not in dates_str or "01-01-{0}".format(annee + 1) not in dates_str:
        raise ValueError(
            "Les bornes de campagne ({0} -> {1}) ne couvrent pas Noël (25/12/{2}) et le "
            "Nouvel An (01/01/{3}) : vérifie campagne_config.".format(
                date_debut_campagne, date_fin_campagne, annee, annee + 1
            )
        )

    df_calendar = pd.DataFrame(columns=[
        "date", "day_of_week", "name_day", "garde", "astreinte",
        "astreinte_double", "astreinte_triple", "vendredi", "samedi",
        "dimanche", "veille_ferie", "ferie", "veille_lundi_ferie",
        "lundi_ferie", "WE_N", "N_conge", "conge",
    ])
    df_calendar["date"] = dates_str
    df_calendar["day_of_week"] = [pd.Timestamp(d).dayofweek for d in df_calendar["date"]]

    mapping = {
        0: ("lun", 0, 0, 0), 1: ("mar", 0, 0, 0), 2: ("mer", 0, 0, 0),
        3: ("jeu", 0, 0, 0), 4: ("ven", 1, 0, 0), 5: ("sam", 0, 1, 0),
        6: ("dim", 0, 0, 1),
    }
    for day, (name, ven, sam, dim) in mapping.items():
        mask = df_calendar["day_of_week"] == day
        df_calendar.loc[mask, "name_day"] = name
        df_calendar.loc[mask, "vendredi"] = ven
        df_calendar.loc[mask, "samedi"] = sam
        df_calendar.loc[mask, "dimanche"] = dim

    df_calendar[["veille_ferie", "ferie", "veille_lundi_ferie", "lundi_ferie"]] = 0

    # jours fériés (+ 1er janvier de l'année suivante)
    list_ferie = set(
        [d[0].strftime("%m-%d-%Y") for d in cal.holidays(annee)]
        + [[d[0].strftime("%m-%d-%Y") for d in cal.holidays(annee + 1)][0]]
    )
    df_calendar.loc[df_calendar["date"].isin(list_ferie), "ferie"] = 1

    for i in df_calendar.loc[df_calendar["date"].isin(list_ferie)].index:
        df_calendar.loc[i - 1, "veille_ferie"] = 1
        if df_calendar["name_day"][i] == "lun":
            df_calendar.loc[i, "lundi_ferie"] = 1
            df_calendar.loc[i - 1, "veille_lundi_ferie"] = 1

    # numérotation des week-ends
    we_n = 1
    for i in range(len(df_calendar) - 1):
        if df_calendar["name_day"][i] == "sam":
            df_calendar.loc[i, "WE_N"] = we_n
            df_calendar.loc[i + 1, "WE_N"] = we_n
            we_n += 1

    # fusion des fériés adjacents avec le numéro de WE voisin
    for i in range(len(df_calendar) - 1):
        if df_calendar["ferie"][i] == 1:
            for jour, j in zip(["lun", "mar", "mer"], [1, 2, 3]):
                if df_calendar["name_day"][i] == jour:
                    df_calendar.loc[i, "WE_N"] = df_calendar["WE_N"][i - j]
            for jour, j in zip(["jeu", "ven"], [2, 1]):
                if df_calendar["name_day"][i] == jour:
                    df_calendar.loc[i, "WE_N"] = df_calendar["WE_N"][i + j]

    # qualification double / triple
    num_astr_triple = []
    for astr_n in df_calendar[df_calendar["ferie"] == 1]["WE_N"]:
        df_calendar.loc[df_calendar["WE_N"] == astr_n, "astreinte_triple"] = 1
        num_astr_triple.append(astr_n)
    for astr_n in df_calendar[~df_calendar["WE_N"].isna()]["WE_N"]:
        if astr_n not in num_astr_triple:
            df_calendar.loc[df_calendar["WE_N"] == astr_n, "astreinte_double"] = 1

    # correctifs : un samedi veille de dimanche férié n'est pas un "vendredi-équivalent",
    # et un dimanche férié n'a pas besoin d'être requalifié "férié" en plus du dimanche
    df_calendar.loc[
        (df_calendar["veille_ferie"] == 1) & (df_calendar["name_day"] == "sam"), "veille_ferie"
    ] = 0
    df_calendar.loc[
        (df_calendar["ferie"] == 1) & (df_calendar["name_day"] == "dim"), "ferie"
    ] = 0

    return df_calendar, annee

# ======================================================================
# 2. Chargement des données
# ======================================================================

def charger_medecins(annee: int, dossier: str = ".") -> pd.DataFrame:
    """Charge la table des médecins (quotas, compteurs) pour `annee`."""
    df_medecin = pd.read_excel(f"{dossier}/calendar_consignes_{annee}.xlsx", index_col=0)
    return df_medecin


def charger_conges(annee: int, dossier: str = ".") -> pd.DataFrame:
    """Charge les congés prévisionnels déclarés pour `annee`."""
    return pd.read_excel(f"{dossier}/conges_medecins_{annee}.xlsx", index_col=0)


def charger_memoire_fetes(annee: int, dossier: str = ".") -> pd.DataFrame:
    """Charge la mémoire inter-années des gardes de Noël / Nouvel An."""
    df_fetes = pd.read_excel(f"{dossier}/calendar_consignes_fetes_{annee}.xlsx", index_col=0)
    return df_fetes.astype(object)


# ----------------------------------------------------------------------
# 2bis. Chargement depuis Supabase (données déjà récupérées en JSON/dicts)
# ----------------------------------------------------------------------
#
# Choix d'architecture : ces fonctions ne font AUCUN appel réseau elles-
# mêmes. Elles reçoivent des listes de dicts telles que le JS (supabase-js,
# qui gère déjà l'authentification et les policies RLS) les aura obtenues
# et transmises à Python. Deux raisons à ce choix :
#   1. Une fois ce script exécuté dans le navigateur via Pyodide, faire de
#      vraies requêtes HTTP depuis Python-en-WebAssembly est nettement plus
#      contraignant que depuis le JS qui gère déjà ça très bien.
#   2. Ça garde une séparation nette : le JS s'occupe de la donnée
#      (authentification, RLS, réseau), le Python s'occupe uniquement du
#      calcul. Rien à changer ici si un jour la source de données change
#      encore.

LIGNES_DF_MEDECIN = [
    "quota_acbl_garde", "quota_acbl_astreinte_j", "vendredi", "samedi", "veille_ferie",
    "veille_lundi_ferie", "ferie", "lundi_ferie", "dimanche", "astreinte", "total_astreinte",
    "total_astreinte_ferie", "total_astreinte_pondere", "total_eq_ven", "total_eq_ven_pondere",
    "total_eq_sam", "total_eq_sam_pondere", "total_garde", "grand_total",
]

TYPES_FETE = ["24dec", "25dec", "31dec", "01ja", "astrNoel", "astrAn"]


def construire_df_medecin(profils: list) -> pd.DataFrame:
    """Construit df_medecin à partir des profils Supabase.

    `profils` : liste de dicts {"initiales", "quota_acbl_garde", "quota_acbl_astreinte_j"},
    un par médecin actif (requête sur `profiles` filtrée sur initiales non nulles).
    """
    medecins = [p["initiales"] for p in profils]
    df_medecin = pd.DataFrame(0, index=LIGNES_DF_MEDECIN, columns=medecins, dtype=object)
    for p in profils:
        df_medecin.loc["quota_acbl_garde", p["initiales"]] = p.get("quota_acbl_garde") or 0
        df_medecin.loc["quota_acbl_astreinte_j", p["initiales"]] = p.get("quota_acbl_astreinte_j") or 0
    return df_medecin


def construire_df_conges(conges: list, annee: int) -> pd.DataFrame:
    """Construit df_conges_medecin à partir des congés Supabase.

    `conges` : liste de dicts {"initiales", "date_debut", "date_fin"}
    (jointure `conges_previsionnels` -> `profiles`, comme dans le panneau admin du HTML).
    `annee` : ajoutée telle quelle sur chaque ligne pour garder la compatibilité
    avec `med_absent`, qui filtre sur une colonne "Année" (héritage du format Excel).
    """
    lignes = [{
        "Nom": c["initiales"],
        "Début congé": c["date_debut"],
        "Fin congé": c["date_fin"],
        "Année": annee,
    } for c in conges]
    return pd.DataFrame(lignes, columns=["Nom", "Début congé", "Fin congé", "Année"])


def construire_df_fetes(memoire: list, medecins: list) -> pd.DataFrame:
    """Construit df_fetes à partir de la table Supabase `memoire_fetes`.

    `memoire` : liste de dicts {"initiales", "type_fete", "derniere_annee"}.
    `medecins` : liste des initiales actives (pour que chacun ait une ligne,
    même sans historique).
    """
    df_fetes = pd.DataFrame(0, index=medecins, columns=TYPES_FETE, dtype=object)
    for m in memoire:
        if m["initiales"] in df_fetes.index and m["type_fete"] in TYPES_FETE:
            df_fetes.loc[m["initiales"], m["type_fete"]] = str(m["derniere_annee"])
    return df_fetes


def exporter_resultats_json(df_calendar: pd.DataFrame, df_medecin: pd.DataFrame, df_fetes: pd.DataFrame) -> dict:
    """Prépare les résultats sous forme de structures JSON-sérialisables, prêtes
    à être renvoyées au JS pour écriture dans Supabase (`gardes_resultats`,
    `memoire_fetes`) et pour affichage (statistiques par médecin).

    Retourne un dict :
      - "gardes" : liste de {"date", "role", "initiales"} -> table gardes_resultats
      - "memoire_fetes" : liste de {"initiales", "type_fete", "derniere_annee"} -> upsert memoire_fetes
      - "statistiques" : {medecin: {ligne: valeur}} -> affichage des stats par médecin
    """
    gardes = []
    for _, row in df_calendar.iterrows():
        for role in ("garde", "astreinte"):
            valeur = row[role]
            if pd.notna(valeur) and valeur not in (0, "error"):
                gardes.append({"date": row["date"], "role": role, "initiales": valeur})

    memoire = []
    for med in df_fetes.index:
        for type_fete in df_fetes.columns:
            valeur = df_fetes.loc[med, type_fete]
            if valeur not in (0, "0"):
                memoire.append({"initiales": med, "type_fete": type_fete, "derniere_annee": str(valeur)})

    lignes_obsoletes = ["liste_date", "vacances_prevues", "absences_prevues", "quota_dimanche",
                         "jour_bip", "veille_lundi_ferie", "dimanche", "lundi_ferie", "total_garde"]
    df_stats = df_medecin.drop(index=[l for l in lignes_obsoletes if l in df_medecin.index])

    return {"gardes": gardes, "memoire_fetes": memoire, "statistiques": df_stats.to_dict()}


def generer_campagne_supabase(profils, conges, memoire_fetes, date_debut_campagne, date_fin_campagne,
                               fenetre_width=4, liste_immunise_debut_annee=None, list_ajout=None,
                               exception_fenetre_width=None):
    """Variante Supabase de `generer_campagne`. Reçoit des données déjà chargées
    (listes de dicts telles que fournies par le JS après requête Supabase) au lieu
    de lire des fichiers Excel.

    Retourne (df_calendar, df_medecin, df_fetes, incidents, anomalies, resultats_json).
    `resultats_json` est prêt à être renvoyé au JS (voir `exporter_resultats_json`).
    """
    df_calendar, annee = generer_calendrier(date_debut_campagne, date_fin_campagne)
    df_medecin = construire_df_medecin(profils)
    df_conges_medecin = construire_df_conges(conges, annee)
    liste_med = df_medecin.columns.tolist()
    df_fetes = construire_df_fetes(memoire_fetes, liste_med)

    if list_ajout:
        df_calendar, df_medecin = positionner_prealablement(df_calendar, df_medecin, list_ajout)

    df_calendar, df_medecin, df_fetes, incidents = generer_repartition_annuelle(
        df_calendar, df_medecin, df_conges_medecin, df_fetes, annee, liste_med,
        fenetre_width=fenetre_width,
        liste_immunise_debut_annee=liste_immunise_debut_annee,
        exception_fenetre_width=exception_fenetre_width,
    )

    anomalies = verifier_coherence(df_calendar, df_medecin, df_conges_medecin, df_fetes, annee)
    resultats = exporter_resultats_json(df_calendar, df_medecin, df_fetes)

    return df_calendar, df_medecin, df_fetes, incidents, anomalies, resultats


def positionner_prealablement(df_calendar: pd.DataFrame, df_medecin: pd.DataFrame, list_ajout: list):
    """Positionne manuellement des gardes/astreintes fixées à l'avance.

    list_ajout : liste de tuples (date "MM-JJ-AAAA", initiales médecin, "garde" ou "astreinte")
    Ne pas utiliser pour un dimanche (géré automatiquement) ou un jour férié
    (à positionner explicitement si besoin).
    """
    for date, med, type_slot in list_ajout:
        df_calendar.loc[df_calendar["date"] == date, type_slot] = med
        if type_slot == "garde":
            if df_calendar.loc[df_calendar["date"] == date, "vendredi"].item() == 1:
                df_medecin.loc["vendredi", med] += 1
            if df_calendar.loc[df_calendar["date"] == date, "samedi"].item() == 1:
                df_medecin.loc["samedi", med] += 1
        if type_slot == "astreinte":
            df_medecin.loc["astreinte", med] += 1
            if df_calendar.loc[df_calendar["date"] == date, "samedi"].item() == 1:
                index_dim = df_calendar.loc[df_calendar["date"] == date].index + 1
                df_calendar.loc[index_dim, type_slot] = med
            else:
                print("Attention : ne donner que des samedis à cette fonction pour une astreinte "
                      "(le dimanche est ajouté automatiquement) ; jours fériés à traiter à part.")
    return df_calendar, df_medecin


# ======================================================================
# 3. Disponibilité et sélection
# ======================================================================

def med_absent(date, df_medecin: pd.DataFrame, liste_exclu: list,
                df_conges_medecin: pd.DataFrame, annee: int) -> list:
    """Retourne `liste_exclu` complétée des médecins en congé déclaré à `date`.

    `date` doit être un pd.Timestamp.
    """
    df_conges = (
        df_conges_medecin[df_conges_medecin["Année"] == annee]
        .reset_index(drop=True)
    )
    for med in df_medecin.columns:
        if med in liste_exclu:
            continue
        for i in range(len(df_conges)):
            if df_conges["Nom"][i] == med:
                periode = pd.date_range(start=df_conges["Début congé"][i], end=df_conges["Fin congé"][i])
                if date in periode:
                    liste_exclu.append(med)
    return liste_exclu


def choix_med(jour: str, liste_med: list, liste_exclu: list, df_medecin: pd.DataFrame):
    """Choisit un médecin pour le type de créneau `jour`, parmi les non-exclus,
    en priorisant celui dont le ratio pondéré (compteur / quota) est le plus bas.
    Tirage aléatoire en cas d'égalité.

    `jour` in {"astreinte", "ferie"/"lundi_ferie"/"dimanche", "veille_ferie"/"vendredi",
               "samedi"/"veille_lundi_ferie"}
    """
    candidats = [med for med in liste_med if med not in liste_exclu]
    df_pond = pd.DataFrame(index=["nb_pondere"])

    if jour == "astreinte":
        for med in candidats:
            if df_medecin[med]["quota_acbl_astreinte_j"] != 0:
                df_pond[med] = df_medecin[med]["total_astreinte_pondere"]

    if jour in ("ferie", "lundi_ferie", "dimanche"):
        for med in candidats:
            if df_medecin[med]["quota_acbl_garde"] != 0:
                # NB : volontairement non pondéré par quota, voir CHANGELOG en tête de fichier.
                df_pond[med] = df_medecin[med]["ferie"]

    if jour in ("veille_ferie", "vendredi"):
        for med in candidats:
            if df_medecin[med]["quota_acbl_garde"] != 0:
                df_pond[med] = df_medecin[med]["total_eq_ven_pondere"]

    if jour in ("samedi", "veille_lundi_ferie"):
        for med in candidats:
            if df_medecin[med]["quota_acbl_garde"] != 0:
                df_pond[med] = df_medecin[med]["total_eq_sam_pondere"]

    df_pond = df_pond[df_pond == df_pond.loc["nb_pondere"].min()].dropna(axis=1)
    if len(df_pond.columns) == 0:
        print("Aucun médecin disponible pour ce créneau. Il faudra réduire la fenêtre "
              "d'immunité à partir de ce week-end et relancer.")
        return "error"
    return random.choice(df_pond.columns)


def recalculer_ponderations(df_medecin: pd.DataFrame) -> pd.DataFrame:
    """Recalcule tous les compteurs pondérés à partir des compteurs bruts.

    Facteur commun à `repartition`, `faire_don_garde`, `faire_don_astreinte`
    et `correction_ponctuelle`, pour ne jamais dupliquer cette logique.
    """
    for med in df_medecin.columns:
        if df_medecin.loc["quota_acbl_astreinte_j", med] != 0:
            df_medecin.loc["total_astreinte_pondere", med] = (
                df_medecin.loc["total_astreinte", med] * 2 + df_medecin.loc["total_astreinte_ferie", med]
            ) / df_medecin.loc["quota_acbl_astreinte_j", med]
        if df_medecin.loc["quota_acbl_garde", med] != 0:
            df_medecin.loc["total_eq_ven", med] = (
                df_medecin.loc["vendredi", med] + df_medecin.loc["veille_ferie", med]
            )
            df_medecin.loc["total_eq_ven_pondere", med] = (
                df_medecin.loc["total_eq_ven", med] / df_medecin.loc["quota_acbl_garde", med]
            )
            df_medecin.loc["total_eq_sam", med] = (
                df_medecin.loc["samedi", med] + df_medecin.loc["veille_lundi_ferie", med]
            )
            df_medecin.loc["total_eq_sam_pondere", med] = (
                df_medecin.loc["total_eq_sam", med] / df_medecin.loc["quota_acbl_garde", med]
            )
    df_medecin.loc["total_garde"] = df_medecin.loc["total_eq_ven_pondere"] + df_medecin.loc["total_eq_sam_pondere"]
    df_medecin.loc["grand_total"] = df_medecin.loc["total_garde"] + df_medecin.loc["total_astreinte_pondere"]
    return df_medecin


# ======================================================================
# 4. Attribution d'un bloc de week-end
# ======================================================================

def repartition(WE_number, fenetre_width, list_immune, df_calendar, df_fetes, df_medecin,
                 df_conges_medecin, annee, liste_med):
    """Attribue toutes les gardes/astreinte du bloc de week-end `WE_number`.

    Retourne aussi `incidents` : liste des créneaux ("ferie", "veille_ferie",
    "astreinte", "vendredi", "samedi") pour lesquels aucun médecin n'était
    disponible (fenêtre trop large / trop de congés ce WE). Ces créneaux
    restent vides dans df_calendar ; relancer ce WE avec une fenêtre plus
    étroite pour les combler.
    """
    incidents = []

    we_max = df_calendar["WE_N"].max()
    we_range = [v for v in range(WE_number - fenetre_width + 1, WE_number + fenetre_width)
                if 1 <= v < we_max]
    for i in we_range:
        list_immune.extend(df_calendar.loc[df_calendar["WE_N"] == i, "astreinte"].tolist())
        s = df_calendar[(df_calendar["WE_N"] == i) & (df_calendar["name_day"] == "sam")].index[0]
        idx_range = [v for v in range(s - 3, s + 4) if 0 <= v < len(df_calendar) - 1]
        list_immune.extend(df_calendar.loc[idx_range, "garde"].tolist())

    ven_index = df_calendar.loc[
        (df_calendar["WE_N"] == WE_number) & (df_calendar["name_day"] == "sam")
    ].index[0] - 1
    sam_index = df_calendar.loc[
        (df_calendar["WE_N"] == WE_number) & (df_calendar["name_day"] == "sam")
    ].index[0]

    bloc_ferie = df_calendar.loc[(df_calendar["WE_N"] == WE_number) & (df_calendar["ferie"] == 1)]
    ferie_index = None
    if not bloc_ferie.empty:
        ferie_index = bloc_ferie.index[0]
        veille_ferie_index = ferie_index - 1

        # garde dédiée du jour férié lui-même : seulement Noël et Nouvel An
        if df_calendar.loc[ferie_index, "date"] == "12-25-{0}".format(annee):
            date_temp = df_calendar.loc[ferie_index, "date"]
            immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
            med_choisi = choix_med("ferie", liste_med,
                                    list_immune + df_fetes[df_fetes["25dec"] != 0].index.tolist() + immune_conge,
                                    df_medecin)
            if med_choisi == "error":
                incidents.append(("ferie_25dec", WE_number))
            else:
                df_fetes.loc[med_choisi, "25dec"] = str(annee)
                list_immune.append(med_choisi)
                df_calendar.loc[ferie_index, "garde"] = med_choisi
                df_medecin.loc["ferie", med_choisi] += 1

        if df_calendar.loc[ferie_index, "date"] == "01-01-{0}".format(annee + 1):
            date_temp = df_calendar.loc[ferie_index, "date"]
            immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
            med_choisi = choix_med("ferie", liste_med,
                                    list_immune + df_fetes[df_fetes["01ja"] != 0].index.tolist() + immune_conge,
                                    df_medecin)
            if med_choisi == "error":
                incidents.append(("ferie_01ja", WE_number))
            else:
                df_fetes.loc[med_choisi, "01ja"] = str(annee + 1)
                list_immune.append(med_choisi)
                df_calendar.loc[ferie_index, "garde"] = med_choisi
                df_medecin.loc["ferie", med_choisi] += 1

        # garde de la veille du férié (équivalent vendredi), avec mémoire pour 24/12 et 31/12
        date_temp = df_calendar.loc[
            (df_calendar["WE_N"] == WE_number) & (df_calendar["name_day"] == "sam"), "date"
        ].values[0]
        immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
        med_choisi = choix_med("veille_ferie", liste_med, list_immune + immune_conge, df_medecin)

        if df_calendar.loc[veille_ferie_index, "date"] == "12-24-{0}".format(annee):
            date_temp = df_calendar.loc[veille_ferie_index, "date"]
            immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
            med_choisi = choix_med("veille_ferie", liste_med,
                                    list_immune + df_fetes[df_fetes["24dec"] != 0].index.tolist() + immune_conge,
                                    df_medecin)
            if med_choisi != "error":
                df_fetes.loc[med_choisi, "24dec"] = str(annee)

        if df_calendar.loc[veille_ferie_index, "date"] == "12-31-{0}".format(annee):
            date_temp = df_calendar.loc[veille_ferie_index, "date"]
            immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
            med_choisi = choix_med("veille_ferie", liste_med,
                                    list_immune + df_fetes[df_fetes["31dec"] != 0].index.tolist() + immune_conge,
                                    df_medecin)
            if med_choisi != "error":
                df_fetes.loc[med_choisi, "31dec"] = str(annee)

        if med_choisi == "error":
            incidents.append(("veille_ferie", WE_number))
        else:
            list_immune.append(med_choisi)
            df_calendar.loc[veille_ferie_index, "garde"] = med_choisi
            df_medecin.loc["veille_ferie", med_choisi] += 1

    # astreinte du bloc, puis garde du vendredi, puis garde du samedi
    for type_slot, selection, ligne_medecin, colonne in zip(
        ["astreinte", "vendredi", "samedi"],
        [df_calendar["WE_N"] == WE_number, ven_index, sam_index],
        ["total_astreinte", "vendredi", "samedi"],
        ["astreinte", "garde", "garde"],
    ):
        date_temp = (df_calendar.loc[ven_index, "date"] if type_slot == "vendredi"
                     else df_calendar.loc[(df_calendar["WE_N"] == WE_number) & (df_calendar["name_day"] == "sam"), "date"].values[0])
        immune_conge = med_absent(pd.Timestamp(date_temp), df_medecin, [], df_conges_medecin, annee)
        med_choisi = choix_med(type_slot, liste_med, list_immune + immune_conge, df_medecin)

        if type_slot == "astreinte" and ferie_index is not None and med_choisi != "error":
            if df_calendar.loc[ferie_index, "date"] == "12-25-{0}".format(annee):
                med_choisi = choix_med("astreinte", liste_med,
                                        list_immune + immune_conge + df_fetes[df_fetes["astrNoel"] != 0].index.tolist(),
                                        df_medecin)
                if med_choisi != "error":
                    df_fetes.loc[med_choisi, "astrNoel"] = str(annee)
            if df_calendar.loc[ferie_index, "date"] == "01-01-{0}".format(annee + 1):
                med_choisi = choix_med("astreinte", liste_med,
                                        list_immune + immune_conge + df_fetes[df_fetes["astrAn"] != 0].index.tolist(),
                                        df_medecin)
                if med_choisi != "error":
                    df_fetes.loc[med_choisi, "astrAn"] = str(annee)

        if med_choisi == "error":
            incidents.append((type_slot, WE_number))
            continue

        list_immune.append(med_choisi)
        if isinstance(df_calendar.loc[selection, colonne], str) or (
            hasattr(df_calendar.loc[selection, colonne], "any")
            and df_calendar.loc[selection, colonne].notna().any()
            and (df_calendar.loc[selection, colonne] != 0).any()
        ):
            print(f"{type_slot} du WE {WE_number} ({colonne}) déjà rempli, ignoré.")
        else:
            df_calendar.loc[selection, colonne] = med_choisi
            df_medecin.loc[ligne_medecin, med_choisi] += 1
        if colonne == "astreinte" and ferie_index is not None:
            df_medecin.loc["total_astreinte_ferie", med_choisi] += 1

    recalculer_ponderations(df_medecin)
    return df_calendar, df_medecin, df_fetes, list_immune, incidents


# ======================================================================
# 5. Boucle principale de génération d'une campagne
# ======================================================================

def generer_repartition_annuelle(df_calendar, df_medecin, df_conges_medecin, df_fetes,
                                  annee, liste_med, fenetre_width=4,
                                  liste_immunise_debut_annee=None,
                                  exception_fenetre_width=None):
    """Génère la répartition complète de la campagne `annee`.

    1. Astreintes de Noël et du Nouvel An en premier (mémoire inter-années).
    2. Les week-ends restants, du plus contraint (le plus de congés) au
       moins contraint.

    `liste_immunise_debut_annee` : médecins déjà de garde en toute fin de
    campagne précédente, à ne pas resolliciter sur les 2 premiers week-ends.
    `exception_fenetre_width` : liste de numéros de WE à partir desquels
    réduire `fenetre_width` de 1 (utilisé si un run précédent a signalé
    qu'aucun médecin n'était disponible à ce WE).
    """
    liste_immunise_debut_annee = liste_immunise_debut_annee or []
    exception_fenetre_width = exception_fenetre_width or []

    deja_annee_davant = df_fetes[
        df_fetes.apply(lambda row: row.astype(str).str.contains(f"{annee-1}|{annee}").any(), axis=1)
    ].index.tolist()

    astr_noel_n = df_calendar.loc[df_calendar["date"] == "12-25-{0}".format(annee), "WE_N"].values[0]
    astr_an_n = df_calendar.loc[df_calendar["date"] == "01-01-{0}".format(annee + 1), "WE_N"].values[0]

    for we_number in [astr_noel_n, astr_an_n]:
        df_calendar, df_medecin, df_fetes, _, _ = repartition(
            we_number, fenetre_width, list(deja_annee_davant), df_calendar, df_fetes,
            df_medecin, df_conges_medecin, annee, liste_med,
        )

    # charge de congés par week-end, pour traiter les plus contraints en premier
    for number in range(1, int(df_calendar["WE_N"].max())):
        for date in df_calendar.loc[df_calendar["WE_N"] == number, "date"]:
            immune = med_absent(pd.Timestamp(date), df_medecin, [], df_conges_medecin, annee)
            df_calendar.loc[df_calendar["date"] == date, "N_conge"] = len(immune)
            df_calendar.loc[df_calendar["date"] == date, "conge"] = str(immune)

    # le vendredi précédant un samedi hérite du même niveau de congé que le samedi
    for i in df_calendar[df_calendar["name_day"] == "sam"].index:
        df_calendar.loc[i - 1, "conge"] = df_calendar.loc[i, "conge"]

    we_order = df_calendar.loc[
        df_calendar[df_calendar["name_day"] == "sam"].sort_values(by="N_conge", ascending=False).index
    ]["WE_N"].values.tolist()
    we_order = [n for n in we_order if n not in (astr_noel_n, astr_an_n)]

    incidents = []  # (type_creneau, numero_WE) où aucun médecin n'était disponible
    for we_number in we_order:
        list_immune = []
        largeur = fenetre_width
        if we_number in (1, 2):
            list_immune.extend(liste_immunise_debut_annee)
        if we_number in exception_fenetre_width:
            largeur = max(1, fenetre_width - 1)
        df_calendar, df_medecin, df_fetes, _, incidents_we = repartition(
            we_number, largeur, list_immune, df_calendar, df_fetes,
            df_medecin, df_conges_medecin, annee, liste_med,
        )
        incidents.extend(incidents_we)

    recalculer_ponderations(df_medecin)
    df_calendar.loc[df_calendar["conge"].isnull(), "conge"] = "[]"

    return df_calendar, df_medecin, df_fetes, incidents


# ======================================================================
# 6. Outils de rétrocontrôle (dons, corrections manuelles)
# ======================================================================

def calendar_search(df_calendar: pd.DataFrame, med: str, type_jour: str) -> pd.DataFrame:
    """Renvoie les dates où `med` occupe le rôle `type_jour` ("garde" ou "astreinte")."""
    return df_calendar.loc[df_calendar[type_jour] == med, ["date", "name_day", "WE_N"]]


def gen_liste_immune(date_a_changer, fenetre_width, list_immune, df_calendar, df_fetes, df_medecin):
    """Reconstruit la liste d'immunité applicable à `date_a_changer`."""
    index_day = df_calendar[df_calendar["date"] == date_a_changer].index
    ti = 0
    while np.isnan(df_calendar.loc[index_day[0] + ti, "WE_N"]):
        ti += 1
    we_number = df_calendar.loc[index_day[0] + ti, "WE_N"]
    we_max = df_calendar["WE_N"].max()
    we_range = [v for v in range(int(we_number) - fenetre_width + 1, int(we_number) + fenetre_width)
                if 1 <= v < we_max]
    for i in we_range:
        list_immune.extend(df_calendar[df_calendar["WE_N"] == i]["astreinte"].tolist())
        s = df_calendar[(df_calendar["WE_N"] == i) & (df_calendar["name_day"] == "sam")].index[0]
        idx_range = [v for v in range(s - 3, s + 4) if 0 <= v < len(df_calendar) - 1]
        list_immune.extend(df_calendar.loc[idx_range, "garde"].tolist())

    conge_ce_jour = df_calendar.loc[df_calendar["date"] == date_a_changer, "conge"].item()
    if isinstance(conge_ce_jour, str) and conge_ce_jour:
        list_immune = list_immune + ast.literal_eval(conge_ce_jour)
    return list_immune


def faire_don_garde(df_calendar, df_medecin, date, donateur, receveur):
    """Transfère une garde (vendredi/samedi/veille de férié/férié/dimanche...)
    d'une date précise du `donateur` vers le `receveur`."""
    idx = df_calendar.loc[df_calendar["date"] == date].index
    actuel = df_calendar.loc[idx, "garde"].values[0]
    if actuel != donateur:
        raise ValueError(f"{donateur} n'est pas affecté à la garde du {date} (trouvé : {actuel!r}).")

    df_calendar.loc[idx, "garde"] = receveur
    for type_jour in ["vendredi", "samedi", "dimanche", "veille_ferie", "ferie", "veille_lundi_ferie", "lundi_ferie"]:
        delta = df_calendar.loc[idx, type_jour].values[0]
        if delta:
            df_medecin.loc[type_jour, donateur] -= delta
            df_medecin.loc[type_jour, receveur] += delta

    recalculer_ponderations(df_medecin)
    return df_calendar, df_medecin


def faire_don_astreinte(df_calendar, df_medecin, WE_number, donateur, receveur):
    """Transfère l'astreinte du week-end `WE_number` du `donateur` vers le `receveur`
    (toutes les dates du bloc, pas seulement un jour isolé)."""
    mask = (df_calendar["WE_N"] == WE_number) & (df_calendar["astreinte"] == donateur)
    if not mask.any():
        raise ValueError(f"{donateur} n'a pas l'astreinte du WE {WE_number}.")

    df_calendar.loc[mask, "astreinte"] = receveur
    df_medecin.loc["total_astreinte", donateur] -= 1
    df_medecin.loc["total_astreinte", receveur] += 1
    if (df_calendar.loc[df_calendar["WE_N"] == WE_number, "ferie"] == 1).any():
        df_medecin.loc["total_astreinte_ferie", donateur] -= 1
        df_medecin.loc["total_astreinte_ferie", receveur] += 1

    recalculer_ponderations(df_medecin)
    return df_calendar, df_medecin


def correction_ponctuelle(df_medecin, medecin, ligne, valeur):
    """Corrige manuellement une cellule de df_medecin (à utiliser avec précaution)
    puis recalcule tous les totaux pondérés."""
    df_medecin.loc[ligne, medecin] += valeur
    return recalculer_ponderations(df_medecin)


def suggerer_dons(df_calendar, df_medecin, df_conges_medecin, df_fetes, annee,
                   fenetre_recherche=2, exclus_de_modif=None, exclude_from_stat=None):
    """Repère les médecins statistiquement au-dessus/en-dessous de la moyenne
    (mean +/- std) sur l'astreinte pondérée et la charge totale de garde, et
    propose des dons compatibles (médecin receveur non exclu par la fenêtre
    d'immunité NI par un congé déclaré à cette date).

    Retourne une liste de dicts, prêts à être passés à `faire_don_garde` /
    `faire_don_astreinte` :
        {"type": "garde"|"astreinte", "date": ..., "WE_N": ..., "jour": ...,
         "donateur": ..., "receveur": ...}
    """
    exclus_de_modif = exclus_de_modif or []
    exclude_from_stat = exclude_from_stat or []
    df_stats = df_medecin.drop(columns=exclude_from_stat, errors="ignore")

    suggestions = []
    deja_vu = set()  # dédoublonne les suggestions d'astreinte (une astreinte = plusieurs lignes de calendrier)

    for titre, keyword in [("total_astreinte_pondere", "astreinte"), ("total_garde", "garde")]:
        serie = df_stats.loc[titre]
        serie = serie[serie != 0]
        if serie.empty:
            continue
        seuil_up = serie.mean() + serie.std()
        seuil_down = serie.mean() - serie.std()

        med_a_diminuer = [m for m in df_medecin.columns
                           if m not in exclus_de_modif and df_medecin.loc[titre, m] >= seuil_up]
        med_a_augmenter = [m for m in df_medecin.columns
                            if m not in exclus_de_modif and df_medecin.loc[titre, m] <= seuil_down]

        for med_dim in med_a_diminuer:
            dates_dim = calendar_search(df_calendar, med_dim, keyword)
            for _, row in dates_dim.iterrows():
                date, jour, we_n = row["date"], row["name_day"], row["WE_N"]
                cle_dedup = (keyword, we_n if keyword == "astreinte" else date, med_dim)
                if keyword == "astreinte" and cle_dedup in deja_vu:
                    continue

                for med_augm in med_a_augmenter:
                    exclus_ce_jour = gen_liste_immune(date, fenetre_recherche, [], df_calendar, df_fetes, df_medecin)
                    exclus_ce_jour = med_absent(pd.Timestamp(date), df_medecin, exclus_ce_jour, df_conges_medecin, annee)
                    if med_augm in exclus_ce_jour:
                        continue
                    suggestions.append({
                        "type": keyword, "date": date, "WE_N": we_n, "jour": jour,
                        "donateur": med_dim, "receveur": med_augm,
                    })
                    if keyword == "astreinte":
                        deja_vu.add(cle_dedup)
                        break  # une seule suggestion suffit par WE d'astreinte

    return suggestions


def verifier_coherence(df_calendar, df_medecin, df_conges_medecin, df_fetes, annee):
    """Contrôles de cohérence a posteriori. Retourne une liste d'anomalies
    (chaînes de texte), vide si tout est cohérent :
    - un médecin de garde/astreinte un jour où il avait déclaré un congé
    - une garde/astreinte de fête répétée sur un médecin déjà attribué les
      années précédentes (d'après df_fetes)
    """
    anomalies = []

    for role in ["astreinte", "garde"]:
        for i in df_calendar[df_calendar[role].notna()].index:
            med = df_calendar.loc[i, role]
            date = df_calendar.loc[i, "date"]
            if med in med_absent(pd.Timestamp(date), df_medecin, [], df_conges_medecin, annee):
                anomalies.append(f"{med} est de {role} le {date} alors qu'un congé est déclaré ce jour-là.")

    for date, col, type_fete, annee_fete in zip(
        ["12-24-{0}".format(annee), "12-25-{0}".format(annee), "12-25-{0}".format(annee),
         "12-31-{0}".format(annee), "01-01-{0}".format(annee + 1), "01-01-{0}".format(annee + 1)],
        ["garde", "garde", "astreinte", "garde", "garde", "astreinte"],
        ["24dec", "25dec", "astrNoel", "31dec", "01ja", "astrAn"],
        [annee, annee, annee, annee, annee + 1, annee],
    ):
        med = df_calendar.loc[df_calendar["date"] == date, col].values
        if len(med) and med[0] in df_fetes[~df_fetes[type_fete].isin([0, str(annee_fete)])].index.tolist():
            anomalies.append(f"{med[0]} a déjà eu {type_fete} une année précédente (le {date}).")

    return anomalies


# ======================================================================
# 7. Export
# ======================================================================

def exporter_resultats(df_calendar, df_medecin, df_fetes, annee, dossier_sortie="."):
    """Exporte le calendrier final, la synthèse par médecin, et met à jour
    la mémoire des fêtes pour l'année suivante."""
    df_calendar.to_excel(f"{dossier_sortie}/calendar_garde_{annee}.xlsx", index=False)

    lignes_obsoletes = ["liste_date", "vacances_prevues", "absences_prevues", "quota_dimanche",
                         "jour_bip", "veille_lundi_ferie", "dimanche", "lundi_ferie", "total_garde"]
    df_medecin.drop(index=[l for l in lignes_obsoletes if l in df_medecin.index]).to_excel(
        f"{dossier_sortie}/calendar_garde_synthese_{annee}.xlsx", index=True
    )
    df_fetes.to_excel(f"{dossier_sortie}/calendar_consignes_fetes_{annee}.xlsx", index=True)


# ======================================================================
# 8. Point d'entrée
# ======================================================================

def generer_campagne(annee, dossier=".", fenetre_width=4,
                      liste_immunise_debut_annee=None, list_ajout=None,
                      exception_fenetre_width=None, exporter=True):
    """Orchestration complète : charge les données, positionne les gardes
    fixées à l'avance, génère la répartition, vérifie la cohérence, exporte.
    """
    date_debut_campagne = "01-02-{0}".format(annee)
    date_fin_campagne = "01-11-{0}".format(annee + 1)  # borne incluse : équivalent à l'ancienne borne exclue du 12/01
    df_calendar, _ = generer_calendrier(date_debut_campagne, date_fin_campagne)
    df_medecin = charger_medecins(annee, dossier)
    df_conges_medecin = charger_conges(annee, dossier)
    df_fetes = charger_memoire_fetes(annee, dossier)
    liste_med = df_medecin.columns.tolist()

    if list_ajout:
        df_calendar, df_medecin = positionner_prealablement(df_calendar, df_medecin, list_ajout)

    df_calendar, df_medecin, df_fetes, incidents = generer_repartition_annuelle(
        df_calendar, df_medecin, df_conges_medecin, df_fetes, annee, liste_med,
        fenetre_width=fenetre_width,
        liste_immunise_debut_annee=liste_immunise_debut_annee,
        exception_fenetre_width=exception_fenetre_width,
    )

    anomalies = verifier_coherence(df_calendar, df_medecin, df_conges_medecin, df_fetes, annee)

    if incidents:
        details = ", ".join(f"{type_creneau} (WE {we})" for type_creneau, we in incidents)
        print(f"ATTENTION : aucun médecin disponible pour : {details}. "
              "Relancer avec exception_fenetre_width incluant ces numéros de WE.")
    if anomalies:
        print("ATTENTION : anomalies détectées :")
        for a in anomalies:
            print(" -", a)

    if exporter:
        exporter_resultats(df_calendar, df_medecin, df_fetes, annee, dossier)

    return df_calendar, df_medecin, df_fetes, incidents, anomalies


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Répartition équitable des gardes/astreintes.")
    parser.add_argument("annee", type=int)
    parser.add_argument("--dossier", default=".")
    parser.add_argument("--fenetre-width", type=int, default=4)
    args = parser.parse_args()

    generer_campagne(args.annee, dossier=args.dossier, fenetre_width=args.fenetre_width)
