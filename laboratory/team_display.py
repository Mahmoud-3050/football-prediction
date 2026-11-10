"""Short display names for clubs (fan / scoreboard style).

Official provider names stay as stored identity. Display names are what the
dashboard shows: Brighton, Bournemouth, Man City, Spurs, Wolves, etc.
"""

from __future__ import annotations

import re

# Overrides keyed by football-data.org official `name`.
# Prefer community / TV short forms over legal club names.
DISPLAY_NAME_BY_OFFICIAL: dict[str, str] = {
    # Premier League / Championship
    "AFC Bournemouth": "Bournemouth",
    "Arsenal FC": "Arsenal",
    "Aston Villa FC": "Aston Villa",
    "Brentford FC": "Brentford",
    "Brighton & Hove Albion FC": "Brighton",
    "Burnley FC": "Burnley",
    "Chelsea FC": "Chelsea",
    "Coventry City FC": "Coventry",
    "Crystal Palace FC": "Crystal Palace",
    "Everton FC": "Everton",
    "Fulham FC": "Fulham",
    "Hull City AFC": "Hull",
    "Ipswich Town FC": "Ipswich",
    "Leeds United FC": "Leeds",
    "Leicester City FC": "Leicester",
    "Liverpool FC": "Liverpool",
    "Luton Town FC": "Luton",
    "Manchester City FC": "Man City",
    "Manchester United FC": "Man Utd",
    "Newcastle United FC": "Newcastle",
    "Nottingham Forest FC": "Nottingham",
    "Sheffield United FC": "Sheffield Utd",
    "Southampton FC": "Southampton",
    "Sunderland AFC": "Sunderland",
    "Tottenham Hotspur FC": "Spurs",
    "West Ham United FC": "West Ham",
    "Wolverhampton Wanderers FC": "Wolves",
    # La Liga
    "Athletic Club": "Athletic",
    "CA Osasuna": "Osasuna",
    "Club Atlético de Madrid": "Atlético",
    "Deportivo Alavés": "Alavés",
    "Elche CF": "Elche",
    "FC Barcelona": "Barcelona",
    "Getafe CF": "Getafe",
    "Girona FC": "Girona",
    "Levante UD": "Levante",
    "Málaga CF": "Málaga",
    "RCD Espanyol de Barcelona": "Espanyol",
    "RCD Mallorca": "Mallorca",
    "RC Celta de Vigo": "Celta",
    "RC Deportivo La Coruña": "Deportivo",
    "Rayo Vallecano de Madrid": "Rayo",
    "Real Betis Balompié": "Betis",
    "Real Madrid CF": "Real Madrid",
    "Real Oviedo": "Oviedo",
    "Real Racing Club de Santander": "Racing",
    "Real Sociedad de Fútbol": "Real Sociedad",
    "Real Valladolid CF": "Valladolid",
    "Sevilla FC": "Sevilla",
    "UD Las Palmas": "Las Palmas",
    "Valencia CF": "Valencia",
    "Villarreal CF": "Villarreal",
    "CD Leganés": "Leganés",
    # Serie A
    "AC Milan": "Milan",
    "AC Monza": "Monza",
    "AC Pisa 1909": "Pisa",
    "ACF Fiorentina": "Fiorentina",
    "AS Roma": "Roma",
    "Atalanta BC": "Atalanta",
    "Bologna FC 1909": "Bologna",
    "Cagliari Calcio": "Cagliari",
    "Como 1907": "Como",
    "Empoli FC": "Empoli",
    "FC Internazionale Milano": "Inter",
    "Frosinone Calcio": "Frosinone",
    "Genoa CFC": "Genoa",
    "Hellas Verona FC": "Verona",
    "Juventus FC": "Juventus",
    "Parma Calcio 1913": "Parma",
    "SS Lazio": "Lazio",
    "SSC Napoli": "Napoli",
    "Torino FC": "Torino",
    "Udinese Calcio": "Udinese",
    "US Cremonese": "Cremonese",
    "US Lecce": "Lecce",
    "US Sassuolo Calcio": "Sassuolo",
    "Venezia FC": "Venezia",
    # Bundesliga
    "1. FC Heidenheim 1846": "Heidenheim",
    "1. FC Köln": "Köln",
    "1. FC Union Berlin": "Union Berlin",
    "1. FSV Mainz 05": "Mainz",
    "Bayer 04 Leverkusen": "Leverkusen",
    "Borussia Dortmund": "Dortmund",
    "Borussia Mönchengladbach": "Gladbach",
    "Eintracht Frankfurt": "Frankfurt",
    "FC Augsburg": "Augsburg",
    "FC Bayern München": "Bayern",
    "FC Schalke 04": "Schalke",
    "FC St. Pauli 1910": "St. Pauli",
    "Hamburger SV": "Hamburg",
    "Holstein Kiel": "Kiel",
    "RB Leipzig": "Leipzig",
    "SC Freiburg": "Freiburg",
    "SC Paderborn 07": "Paderborn",
    "SV 07 Elversberg": "Elversberg",
    "SV Werder Bremen": "Werder",
    "TSG 1899 Hoffenheim": "Hoffenheim",
    "VfB Stuttgart": "Stuttgart",
    "VfL Bochum 1848": "Bochum",
    "VfL Wolfsburg": "Wolfsburg",
    # Ligue 1
    "AJ Auxerre": "Auxerre",
    "Angers SCO": "Angers",
    "AS Monaco FC": "Monaco",
    "AS Saint-Étienne": "Saint-Étienne",
    "ES Troyes AC": "Troyes",
    "FC Lorient": "Lorient",
    "FC Metz": "Metz",
    "FC Nantes": "Nantes",
    "Le Havre AC": "Le Havre",
    "Le Mans FC": "Le Mans",
    "Lille OSC": "Lille",
    "Montpellier HSC": "Montpellier",
    "OGC Nice": "Nice",
    "Olympique de Marseille": "Marseille",
    "Olympique Lyonnais": "Lyon",
    "Paris FC": "Paris FC",
    "Paris Saint-Germain FC": "PSG",
    "Racing Club de Lens": "Lens",
    "RC Strasbourg Alsace": "Strasbourg",
    "Stade Brestois 29": "Brest",
    "Stade de Reims": "Reims",
    "Stade Rennais FC 1901": "Rennes",
    "Toulouse FC": "Toulouse",
    # Eredivisie
    "ADO Den Haag": "Den Haag",
    "AFC Ajax": "Ajax",
    "Almere City FC": "Almere",
    "AZ": "AZ",
    "FC Groningen": "Groningen",
    "FC Twente '65": "Twente",
    "FC Utrecht": "Utrecht",
    "FC Volendam": "Volendam",
    "Feyenoord Rotterdam": "Feyenoord",
    "Fortuna Sittard": "Sittard",
    "Go Ahead Eagles": "Go Ahead",
    "Heracles Almelo": "Heracles",
    "NAC Breda": "NAC",
    "NEC": "NEC",
    "PEC Zwolle": "Zwolle",
    "PSV": "PSV",
    "RKC Waalwijk": "Waalwijk",
    "SBV Excelsior": "Excelsior",
    "SC Cambuur-Leeuwarden": "Cambuur",
    "SC Heerenveen": "Heerenveen",
    "Sparta Rotterdam": "Sparta",
    "Telstar 1963": "Telstar",
    "Willem II Tilburg": "Willem II",
    # Liga Portugal
    "Académico de Viseu FC": "Viseu",
    "Casa Pia AC": "Casa Pia",
    "CD Nacional": "Nacional",
    "CD Santa Clara": "Santa Clara",
    "CD Tondela": "Tondela",
    "CF Estrela da Amadora": "Estrela",
    "CS Marítimo": "Marítimo",
    "FC Alverca": "Alverca",
    "FC Arouca": "Arouca",
    "FC Famalicão": "Famalicão",
    "FC Porto": "Porto",
    "GD Estoril Praia": "Estoril",
    "Gil Vicente FC": "Gil Vicente",
    "Moreirense FC": "Moreirense",
    "Rio Ave FC": "Rio Ave",
    "SC Farense": "Farense",
    "Sport Lisboa e Benfica": "Benfica",
    "Sporting Clube de Braga": "Braga",
    "Sporting Clube de Portugal": "Sporting",
    "Vitória SC": "Vitória",
    # CL / other common
    "AC Sparta Praha": "Sparta Prague",
    "BSC Young Boys": "Young Boys",
    "Celtic FC": "Celtic",
    "Club Brugge KV": "Club Brugge",
    "FC København": "Copenhagen",
    "FC Red Bull Salzburg": "Salzburg",
    "Fenerbahçe SK": "Fenerbahçe",
    "FK Bodø/Glimt": "Bodø/Glimt",
    "FK Crvena Zvezda": "Red Star",
    "FK Kairat": "Kairat",
    "FK Shakhtar Donetsk": "Shakhtar",
    "Galatasaray SK": "Galatasaray",
    "GNK Dinamo Zagreb": "Dinamo Zagreb",
    "LASK Linz": "LASK",
    "PAE AEK": "AEK",
    "PAE Olympiakos SFP": "Olympiacos",
    "Paphos FC": "Paphos",
    "Qarabağ Ağdam FK": "Qarabağ",
    "Royale Union Saint-Gilloise": "Union SG",
    "Sabah FK": "Sabah",
    "SK Slavia Praha": "Slavia Prague",
    "SK Sturm Graz": "Sturm Graz",
    "ŠK Slovan Bratislava": "Slovan Bratislava",
    "Viking FK": "Viking",
}

# Cleanup for provider `shortName` when no official override exists.
_SHORT_NAME_CLEANUPS: dict[str, str] = {
    "Brighton Hove": "Brighton",
    "Man United": "Man Utd",
    "Barça": "Barcelona",
    "Atleti": "Atlético",
    "M'gladbach": "Gladbach",
    "Olympique Lyon": "Lyon",
    "Stade Rennais": "Rennes",
    "Angers SCO": "Angers",
    "Sevilla FC": "Sevilla",
    "SL Benfica": "Benfica",
    "Sporting CP": "Sporting",
    "Estoril Praia": "Estoril",
    "Shaktar": "Shakhtar",
    "Sl. Bratislava": "Slovan Bratislava",
    "1. FC Köln": "Köln",
    "HSV": "Hamburg",
    "Bremen": "Werder",
    "SC Paderborn": "Paderborn",
    "Rayo Vallecano": "Rayo",
    "Santander": "Racing",
    "Como 1907": "Como",
    "Venezia FC": "Venezia",
    "Paris FC": "Paris FC",
    "RC Lens": "Lens",
    "Acad. Viseu": "Viseu",
    "Vitória SC": "Vitória",
    "Amadora": "Estrela",
    "PAE AEK": "AEK",
}

_TRAILING_CLUB = re.compile(
    r"""
    \s+
    (
        FC|CF|AFC|SC|AC|BC|SK|FK|KV|UD|OSC|HSC|CFC|SCO|
        Calcio|Club
    )
    $
    """,
    re.IGNORECASE | re.VERBOSE,
)
_LEADING_CLUB = re.compile(r"^(AFC|FC|CF|AC|SC)\s+", re.IGNORECASE)
_YEAR_TAIL = re.compile(r"\s+\d{4}$")
_NUMBERED_PREFIX = re.compile(r"^\d+\.\s+")


def resolve_display_name(official_name: str, short_name: str | None = None) -> str:
    """Pick a short label for UI: curated → cleaned API shortName → heuristic."""

    name = (official_name or "").strip()
    if not name:
        return ""
    if name in DISPLAY_NAME_BY_OFFICIAL:
        return DISPLAY_NAME_BY_OFFICIAL[name]
    short = (short_name or "").strip()
    if short:
        return _SHORT_NAME_CLEANUPS.get(short, short)
    return _heuristic_short(name)


def _heuristic_short(name: str) -> str:
    text = name.strip()
    text = _NUMBERED_PREFIX.sub("", text)
    text = _LEADING_CLUB.sub("", text)
    text = _TRAILING_CLUB.sub("", text)
    text = _YEAR_TAIL.sub("", text)
    text = re.sub(r"\s+&\s+Hove Albion$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Wanderers$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Hotspur$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+Balompié$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+de Fútbol$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" -")
    return text or name
