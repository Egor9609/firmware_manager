"""parser.py — парсер имён файлов и текстовых описаний прошивок.

Содержит:
    * FirmwareMeta        — dataclass с метаданными прошивки;
    * FirmwareNameParser  — разбор пути/имени файла;
    * TextDescriptionParser — сопоставление текстового описания
                              с файлами прошивок.

Модуль не читает и не изменяет файлы — работает только со строками.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Модель метаданных
# ---------------------------------------------------------------------------


@dataclass
class FirmwareMeta:
    """Метаданные одной прошивки."""

    brand: Optional[str] = None
    model: Optional[str] = None
    engine: Optional[str] = None
    ecu_name: Optional[str] = None
    ecu_id: Optional[str] = None
    author: Optional[str] = None
    eco_standard: Optional[str] = None
    version_tag: Optional[str] = None
    extra_tags: List[str] = field(default_factory=list)
    has_gbo: bool = False
    is_tune: bool = False
    is_stock: bool = False
    imm_off: bool = False
    year: Optional[int] = None

    def to_dict(self) -> Dict[str, object]:
        """Словарь только с заполненными полями (для БД/JSON)."""
        data: Dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "extra_tags":
                value = ", ".join(value) if value else None
            if value not in (None, "", False, 0):
                data[item.name] = value
        return data

    def fill_missing(self, other: "FirmwareMeta") -> None:
        """Дополняет пустые поля значениями из other."""
        for item in fields(self):
            name = item.name
            current = getattr(self, name)
            candidate = getattr(other, name)
            if name == "extra_tags":
                for tag in candidate:
                    if tag not in current:
                        current.append(tag)
                continue
            if isinstance(current, bool) or isinstance(candidate, bool):
                setattr(self, name, bool(current) or bool(candidate))
                continue
            if current in (None, "", 0) and candidate not in (None, "", 0):
                setattr(self, name, candidate)


# ---------------------------------------------------------------------------
# Справочники марок и моделей
# ---------------------------------------------------------------------------

BRAND_ALIASES: Dict[str, List[str]] = {
    "ВАЗ (LADA)": [
        "ваз", "vaz", "lada", "лада", "жигули", "zhiguli", "автоваз",
        "avtovaz", "приора", "priora", "гранта", "granta", "калина",
        "kalina", "веста", "vesta", "нива", "niva", "ларгус", "largus",
        "xray", "x-ray", "иксрей", "икс рей", "vs", "январь", "january",
        "классика", "classic", "я72", "я 72", "я5", "я 5",
    ],
    "Nissan": [
        "nissan", "ниссан", "qashqai", "кашкай", "x-trail", "xtrail",
        "almera", "альмера", "juke", "жук", "teana", "тиана", "patrol",
        "патрол", "micra", "микра", "murano", "мурано", "navara", "навара",
        "terrano", "террано", "skyline", "скайлайн",
    ],
    "Infiniti": ["infiniti", "инфинити", "fx35", "fx37", "qx70", "qx56", "ex35"],
    "Toyota": [
        "toyota", "тойота", "camry", "камри", "corolla", "королла", "rav4",
        "рав4", "land cruiser", "ленд крузер", "лэнд крузер", "prado",
        "прадо", "hilux", "хайлюкс", "avensis", "авенсис", "yaris",
        "ярвис", "highlander", "хайлендер", "prius", "приус",
    ],
    "BMW": ["bmw", "бмв", "bayerische"],
    "Mercedes-Benz": ["mercedes", "мерседес", "мерс", "бенц", "benz", "mb", "viano", "vito", "вито"],
    "Renault": [
        "renault", "рено", "logan", "логан", "sandero", "сандеро", "duster",
        "дастер", "daster", "kaptur", "каптур", "arkana", "аркана", "megane", "меган",
        "scenic", "сценик", "clio", "клио", "fluence", "флюенс", "laguna",
        "лагуна", "symbol", "символ",
    ],
    "Hyundai": [
        "hyundai", "хендай", "хундай", "хендэ", "solaris", "солярис",
        "creta", "крета", "tucson", "туссан", "santa fe", "санта фе",
        "accent", "акцент", "elantra", "элантра", "i30", "i40", "ix35",
        "getz", "гетц", "hiunday", "хендай", "hyndai", "hundai", "hunday",
        "хёндай", "хендэ",
    ],
    "Kia": [
        "kia", "киа", "rio", "рио", "sportage", "спортейдж", "спортаж", "cerato",
        "церато", "sorento", "соренто", "optima", "оптима", "ceed", "сид",
        "carnival", "карнивал", "soul", "соул", "seltos", "селтос",
        "spectra", "спектра", "magentis", "маджентис",
    ],
    "Volkswagen": [
        "volkswagen", "фольксваген", "vw", "vag", "passat", "пассат", "golf",
        "гольф", "polo", "поло", "jetta", "джетта", "tiguan", "тигуан",
        "touran", "турана", "transporter", "транспортер", "crafter",
        "крафтер", "amarok", "амарок",
    ],
    "Skoda": [
        "skoda", "шкода", "octavia", "октавия", "fabia", "фабия", "rapid",
        "рапид", "superb", "суперб", "yeti", "йети", "kodiaq", "кодиак",
    ],
    "Audi": ["audi", "ауди", "a3", "a4", "a6", "q5", "q7"],
    "Ford": [
        "ford", "форд", "focus", "фокус", "mondeo", "мондео", "fiesta",
        "фиеста", "transit", "транзит", "kuga", "куга", "explorer",
        "эксплорер", "ranger", "рейнджер", "ff3", "14c204", "bv61",
        "cv61", "dv61",
    ],
    "Chevrolet": [
        "chevrolet", "шевроле", "chevy", "cruze", "круз", "lacetti",
        "лачетти", "aveo", "авео", "captiva", "каптива", "orlando",
        "орландо", "epica", "эпика", "optra", "rezzo", "tacuma",
    ],
    "Opel": [
        "opel", "опель", "astra", "астра", "corsa", "корса", "insignia",
        "инсигния", "vectra", "вектра", "zafira", "зафира", "mokka",
        "мокка", "vivaro", "виваро",
    ],
    "Mitsubishi": [
        "mitsubishi", "мицубиси", "lancer", "лансер", "outlander",
        "аутлендер", "pajero", "паджеро", "asx", "galant", "галант",
        "l200", "л200", "carisma", "каризма",
    ],
    "Mazda": [
        "mazda", "мазда", "cx-5", "cx5", "cx-7", "cx7", "cx-9", "cx9",
        "mazda3", "mazda6",
    ],
    "Honda": [
        "honda", "хонда", "accord", "аккорд", "civic", "цивик", "cr-v",
        "crv", "fit", "фит", "pilot", "пилот",
    ],
    "Subaru": [
        "subaru", "субару", "forester", "форестер", "impreza", "импреза",
        "outback", "аутбэк", "legacy", "легаси",
    ],
    "Suzuki": [
        "suzuki", "сузуки", "grand vitara", "гранд витара", "sx4",
        "s-cross", "jimny", "джимни", "liana", "лиана",
    ],
    "Peugeot": [
        "peugeot", "пежо", "partner", "партнер", "boxer", "боксер",
        "308", "408", "508", "3008", "5008",
    ],
    "Citroen": [
        "citroen", "ситроен", "berlingo", "берлинго", "jumper", "джампер",
        "c4", "c5", "c3",
    ],
    "Daewoo": [
        "daewoo", "дэу", "деу", "nexia", "нексия", "matiz", "матиз",
        "sens", "сенс", "lanos", "ланос", "espero", "эсперо", "evanda", "эванда",
    ],
    "Chery": [
        "chery", "чери", "tiggo", "тигго", "amulet", "амулет", "fora",
        "фора", "bonus", "бонус", "indis", "индис",
    ],
    "Geely": [
        "geely", "джили", "emgrand", "эмгранд", "coolray", "кулрей",
        "atlas", "атлас", "tugella", "тугелла", "monjaro", "монжаро",
    ],
    "Haval": [
        "haval", "хавал", "jolion", "джулион", "f7", "h6", "h9",
        "dargo", "дарго",
    ],
    "Lifan": [
        "lifan", "лифан", "solano", "солано", "x50", "x60", "x70",
        "smily", "смайли",
    ],
    "UAZ": [
        "uaz", "уаз", "patriot", "патриот", "hunter", "хантер",
        "буханка", "bukhanka", "profi", "профи",
    ],
    "GAZ": [
        "gaz", "газ", "газель", "gazelle", "sobol", "соболь", "volga", "волга",
        "umz", "умз",
    ],
    "SsangYong": [
        "ssangyong", "ссангйонг", "actyon", "актион", "kyron", "кайрон",
        "rexton", "ректсон", "stavic", "ставик",
    ],
    "Datsun": ["datsun", "датсун", "on-do", "он-до", "mi-do", "ми-до"],
    "Land Rover": [
        "land rover", "ленд ровер", "range rover", "рейндж ровер",
        "discovery", "дискавери", "defender", "дефендер",
    ],
    "Volvo": ["volvo", "вольво", "xc60", "xc90", "s40", "s60", "s80"],
    "Jeep": [
        "jeep", "джип", "grand cherokee", "гранд чероки", "wrangler",
        "ренглер",
    ],
    "Fiat": ["fiat", "фиат", "ducato", "дукато", "doblo", "добло"],
    "Iveco": ["iveco", "ивеко", "daily", "дейли"],
    "Scania": ["scania", "скания"],
    "Daf": ["daf", "даф"],
    "Great Wall": [
        "great wall", "greatwall", "gwm", "hover", "wingle", "sailor", "poer",
    ],
    "Brilliance": ["brilliance", "h530", "h230", "h320", "frv"],
    "JAC": ["jac", "rein", "jac j5", "jac s3"],
    "Changan": ["changan", "чанган", "cs35", "cs55", "cs75", "eado"],
    "Exeed": ["exeed", "эксид", "txl", "vx"],
    "Foton": ["foton", "фотон", "tunland"],
    "Dongfeng": ["dongfeng", "донгфенг", "донгфэн"],
    "Alfa Romeo": ["alfa romeo", "alfa", "альфа ромео", "giulietta", "mito", "brera"],
    "Lancia": ["lancia", "лянча", "lybra", "ypsilon", "delta"],
    "Chrysler": ["chrysler", "крайслер", "sebring", "300c", "pt cruiser"],
    "Dodge": ["dodge", "додж", "durango", "caliber", "journey"],
    "Smart": ["smart fortwo", "smart", "смарт", "fortwo"],
    "Porsche": ["porsche", "porshe", "порше", "cayenne", "кайен", "macan", "maccan", "panamera"],
    "Isuzu": ["isuzu", "исузу", "elf", "d max"],
    "John Deere": ["john deere", "johndeere", "deere", "7210r", "6155", "6150"],
    "KAMAZ": ["kamaz", "камаз"],
    "МТЗ": ["мтз", "mtz", "belarus", "беларус"],
    "MAN": ["man tgs", "man tgx", "man tgl", "man tgm"],
    "New Holland": ["new holland", "newholland", "нью холланд"],
    "Case": ["case ih", "caseih", "case maxxum", "case puma"],
    "Kubota": ["kubota", "кубота"],
    "JCB": ["jcb", "fastrac"],
    "Dacia": ["dacia", "дачия", "дация"],
    "PAZ": ["paz", "паз"],
    "Kraz": ["kraz", "краз"],
    "Derways": ["derways", "дервейс", "aurora", "аврора"],
    "Hafei": ["hafei", "хафей"],
    "Ravon": ["ravon", "равон"],
    "ZAZ": ["zaz", "заз", "forza", "форца", "slavuta", "славута", "chance", "шанс", "vida", "вида"],
    "Haima": ["haima", "хайма"],
    "SEAT": ["seat", "сеат", "loen", "leon", "ibiza", "toledo"],
    "MINI": ["mini", "мини", "cooper", "clubman", "countryman"],
    "BYD": ["byd", "бид", "f3"],
    "Vortex": ["vortex", "вортекс", "corda", "корда"],
    "Changhe": ["changhe", "чангхэ", "changhe ideal"],
    "SMA": ["sma", "сма"],
}

MODEL_ALIASES: Dict[str, List[str]] = {
    "Granta": ["granta", "гранта"],
    "Priora": ["priora", "приора"],
    "Kalina": ["kalina", "калина"],
    "Vesta": ["vesta", "веста"],
    "Niva": ["niva", "нива"],
    "Largus": ["largus", "ларгус"],
    "XRAY": ["xray", "x-ray", "икс рей", "иксрей"],
    "Logan": ["logan", "логан"],
    "Sandero": ["sandero", "сандеро"],
    "Duster": ["duster", "дастер", "daster"],
    "Kaptur": ["kaptur", "каптур"],
    "Arkana": ["arkana", "аркана"],
    "Megane": ["megane", "меган"],
    "Scenic": ["scenic", "сценик"],
    "Clio": ["clio", "клио"],
    "Fluence": ["fluence", "флюенс"],
    "Laguna": ["laguna", "лагуна"],
    "Qashqai": ["qashqai", "кашкай"],
    "X-Trail": ["x trail", "xtrail", "икс трейл"],
    "Almera": ["almera", "альмера"],
    "Juke": ["juke", "жук"],
    "Teana": ["teana", "тиана"],
    "Patrol": ["patrol", "патрол"],
    "Micra": ["micra", "микра"],
    "Murano": ["murano", "мурано"],
    "Terrano": ["terrano", "террано"],
    "Navara": ["navara", "навара"],
    "Camry": ["camry", "камри"],
    "Corolla": ["corolla", "королла"],
    "RAV4": ["rav4", "rav 4", "рав4", "рав 4"],
    "Land Cruiser": [
        "land cruiser", "ленд крузер", "лэнд крузер", "landcruiser",
    ],
    "Prado": ["prado", "прадо"],
    "Hilux": ["hilux", "хайлюкс"],
    "Avensis": ["avensis", "авенсис"],
    "Yaris": ["yaris", "ярвис"],
    "Highlander": ["highlander", "хайлендер"],
    "Prius": ["prius", "приус"],
    "Passat": ["passat", "пассат"],
    "Golf": ["golf", "гольф"],
    "Polo": ["polo", "поло"],
    "Jetta": ["jetta", "джетта"],
    "Tiguan": ["tiguan", "тигуан"],
    "Touran": ["touran", "турана"],
    "Transporter": ["transporter", "транспортер"],
    "Crafter": ["crafter", "крафтер"],
    "Amarok": ["amarok", "амарок"],
    "Octavia": ["octavia", "октавия"],
    "Fabia": ["fabia", "фабия"],
    "Rapid": ["rapid", "рапид"],
    "Superb": ["superb", "суперб"],
    "Yeti": ["yeti", "йети"],
    "Kodiaq": ["kodiaq", "кодиак"],
    "Astra": ["astra", "астра"],
    "Corsa": ["corsa", "корса"],
    "Insignia": ["insignia", "инсигния"],
    "Vectra": ["vectra", "вектра"],
    "Zafira": ["zafira", "зафира"],
    "Mokka": ["mokka", "мокка"],
    "Vivaro": ["vivaro", "виваро"],
    "Focus": ["focus", "фокус"],
    "Mondeo": ["mondeo", "мондео"],
    "Fiesta": ["fiesta", "фиеста"],
    "Transit": ["transit", "транзит"],
    "Kuga": ["kuga", "куга"],
    "Explorer": ["explorer", "эксплорер"],
    "Ranger": ["ranger", "рейнджер"],
    "Cruze": ["cruze", "круз"],
    "Lacetti": ["lacetti", "лачетти"],
    "Aveo": ["aveo", "авео"],
    "Captiva": ["captiva", "каптива"],
    "Orlando": ["orlando", "орландо"],
    "Epica": ["epica", "эпика"],
    "Optra": ["optra", "оптра"],
    "Rezzo": ["rezzo", "реззо"],
    "Tacuma": ["tacuma", "такума"],
    "Lancer": ["lancer", "лансер"],
    "Outlander": ["outlander", "аутлендер"],
    "Pajero": ["pajero", "паджеро"],
    "ASX": ["asx", "асх"],
    "Galant": ["galant", "галант"],
    "L200": ["l200", "л200"],
    "Carisma": ["carisma", "каризма"],
    "Solaris": ["solaris", "солярис"],
    "Creta": ["creta", "крета"],
    "Tucson": ["tucson", "туссан"],
    "Santa Fe": ["santa fe", "санта фе"],
    "Accent": ["accent", "акцент"],
    "Elantra": ["elantra", "элантра"],
    "Getz": ["getz", "гетц"],
    "i30": ["i30", "i 30", "ай 30"],
    "i40": ["i40", "i 40"],
    "i20": ["i20", "i 20"],
    "i10": ["i10", "i 10"],
    "ix35": ["ix35", "ix 35"],
    "ix55": ["ix55", "ix 55"],
    "Sonata": ["sonata", "соната"],
    "Matrix": ["matrix", "матрикс"],
    "Starex": ["starex", "старекс"],
    "H-1": ["h-1", "h 1", "h1"],
    "Genesis": ["genesis", "генезис"],
    "Kona": ["kona", "кона"],
    "Venue": ["venue", "веню"],
    "Ioniq": ["ioniq", "ионик"],
    "Bayon": ["bayon", "байон"],
    "Picanto": ["picanto", "пиканто"],
    "Stinger": ["stinger", "стингер"],
    "Xceed": ["xceed", "xc"],
    "Proceed": ["proceed"],
    "Niro": ["niro", "ниро"],
    "Mohave": ["mohave", "мохаве"],
    "K5": ["k5"],
    "K7": ["k7"],
    "Venga": ["venga", "венга"],
    "Carens": ["carens", "каренс"],
    "Viano": ["viano", "виано"],
    "Forza": ["forza", "форца"],
    "Slavuta": ["slavuta", "славута"],
    "Chance": ["chance", "шанс"],
    "Vida": ["vida", "вида"],
    "Spectra": ["spectra", "спектра"],
    "Magentis": ["magentis", "маджентис"],
    "Leon": ["leon", "леон", "loen"],
    "Ibiza": ["ibiza", "ибица"],
    "Toledo": ["toledo", "толедо"],
    "Cooper": ["cooper", "купер"],
    "Clubman": ["clubman"],
    "Countryman": ["countryman"],
    "Macan": ["macan", "maccan", "макан"],
    "Cayenne": ["cayenne", "кайен"],
    "Panamera": ["panamera", "панамера"],
    "Cayman": ["cayman"],
    "Boxster": ["boxster"],
    "Haima 3": ["haima 3", "haima3"],
    "Haima 7": ["haima 7", "haima7"],
    "F3": ["f3"],
    "Corda": ["corda", "корда"],
    "Ideal": ["ideal", "идеал"],
    "Rio": ["rio", "рио"],
    "Sportage": ["sportage", "спортейдж", "спортаж"],
    "Cerato": ["cerato", "церато"],
    "Sorento": ["sorento", "соренто"],
    "Optima": ["optima", "оптима"],
    "Ceed": ["ceed", "сид"],
    "Carnival": ["carnival", "карнивал"],
    "Soul": ["soul", "соул"],
    "Seltos": ["seltos", "селтос"],
    "Patriot": ["patriot", "патриот"],
    "Hunter": ["hunter", "хантер"],
    "Gazelle": ["gazelle", "газель"],
    "Sobol": ["sobol", "соболь"],
    "Nexia": ["nexia", "нексия"],
    "Gentra": ["gentra", "гентра"],
    "Matiz": ["matiz", "матиз"],
    "Lanos": ["lanos", "ланос"],
    "Sens": ["sens", "сенс"],
    "Espero": ["espero", "эсперо"],
    "Evanda": ["evanda", "эванда"],
    "Tiggo": ["tiggo", "тигго"],
    "Amulet": ["amulet", "амулет"],
    "Fora": ["fora", "фора"],
    "Bonus": ["bonus", "бонус"],
    "Emgrand": ["emgrand", "эмгранд"],
    "Coolray": ["coolray", "кулрей"],
    "Atlas": ["atlas", "атлас"],
    "Tugella": ["tugella", "тугелла"],
    "Monjaro": ["monjaro", "монжаро"],
    "Jolion": ["jolion", "джулион"],
    "Dargo": ["dargo", "дарго"],
    "Solano": ["solano", "солано"],
    "Actyon": ["actyon", "актион"],
    "Kyron": ["kyron", "кайрон"],
    "Rexton": ["rexton", "ректсон"],
    "Stavic": ["stavic", "ставик"],
    "CX-5": ["cx 5", "cx5"],
    "CX-7": ["cx 7", "cx7"],
    "CX-9": ["cx 9", "cx9"],
    "Accord": ["accord", "аккорд"],
    "Civic": ["civic", "цивик"],
    "CR-V": ["cr v", "crv"],
    "Forester": ["forester", "форестер"],
    "Impreza": ["impreza", "импреза"],
    "Outback": ["outback", "аутбэк"],
    "Legacy": ["legacy", "легаси"],
    "Grand Vitara": ["grand vitara", "гранд витара"],
    "Jimny": ["jimny", "джимни"],
    "Liana": ["liana", "лиана"],
    "Berlingo": ["berlingo", "берлинго"],
    "Partner": ["partner", "партнер"],
    "Boxer": ["boxer", "боксер"],
    "Jumper": ["jumper", "джампер"],
    "Ducato": ["ducato", "дукато"],
    "Doblo": ["doblo", "добло"],
    "Daily": ["daily", "дейли"],
    "Defender": ["defender", "дефендер"],
    "Discovery": ["discovery", "дискавери"],
    "Wrangler": ["wrangler", "ренглер"],
    "Grand Cherokee": ["grand cherokee", "гранд чероки"],
    "Sprinter": ["sprinter", "спринтер"],
    "Actros": ["actros", "актрос"],
}

# ---------------------------------------------------------------------------
# Справочники ЭБУ
# ---------------------------------------------------------------------------

ECU_RULES: List[Tuple[str, str]] = [
    (r"\bянварь[\s\-_]*(?:5[\s\-_]*\.?[\s\-_]*1)\b|\bvs[\s\-_]*5\.1\b|\bя5\.1\b", "Январь 5.1"),
    (r"\bянварь[\s\-_]*(?:7[\s\-_]*\.?[\s\-_]*2)\b|\bvs[\s\-_]*7\.2\b|\bя7\.2\b", "Январь 7.2"),
    (r"(?<![0-9a-zа-яё])[mм][\s\-_]*74[\s\-_]*\.?[\s\-_]*9(?:[\s\-_]+|\.)[\s\-_]*1(?![0-9])", "М74.9.1"),
    (r"(?<![0-9a-zа-яё])[mм][\s\-_]*74[\s\-_]*\.?[\s\-_]*91(?![0-9])", "М74.91"),
    (r"(?<![0-9a-zа-яё])[mм][\s\-_]*74[\s\-_]*[mм](?![0-9a-zа-яё])", "М74М"),
    (r"\bm[\s\-_]*74[\s\-_]*\.?[\s\-_]*9\b|\bм[\s\-_]*74[\s\-_]*\.?[\s\-_]*9\b", "М74.9"),
    (r"\bm[\s\-_]*74[\s\-_]*\.?[\s\-_]*5\b|\bм[\s\-_]*74[\s\-_]*\.?[\s\-_]*5\b", "М74.5"),
    (r"\bm[\s\-_]*74\b|\bм[\s\-_]*74\b", "М74"),
    (r"\bm[\s\-_]*73\b|\bм[\s\-_]*73\b", "М73"),
    (r"\bm[\s\-_]*75\b|\bм[\s\-_]*75\b", "М75"),
    (r"\bm[\s\-_]*86\b|\bм[\s\-_]*86\b", "М86"),
    (r"\bmikas[\s\-_]*(?:7\.6|10\.3|11\.1)\b|\bмикас[\s\-_]*(?:7\.6|10\.3|11\.1)\b", "Микас"),
    # Микас 11.4: M114_K66, М114_GB_042, M114151GN293060 (УАЗ/ГАЗ "IRON").
    (r"(?<![0-9A-Za-zА-Яа-я])[mм][\s\-_]*114(?![A-Za-zА-Яа-я])", "Микас 11.4"),
    (r"(?<![0-9a-zа-яё])(?:m|м)[\s\-_]*(?:10\.3|11\.4)(?![0-9])", "Микас 10.3/11.4"),
    (r"(?<![0-9a-zа-яё])melco(?![0-9a-zа-яё])", "Melco"),
    (r"(?<![0-9a-zа-яё])simos(?![0-9a-zа-яё])", "Simos"),
    # Папка "Я_5.х.х" — Январь 5.1.
    (r"(?<![0-9A-Za-zА-Яа-я])[яЯ][\s\-_]*5[\s\-_]*\.[\s\-_]*[0-9хxXХ]", "Январь 5.1"),
    (r"\bм[\s\-_]*(?:7\.6|10\.3|11\.1)\b", "Микас"),
    # "Микас11ЕТ", "Микас 12", "M12.4", "M12_UE2009" — слитно, без пробела.
    (r"\b(?:mikas|микас)[\s\-_]*\d+[a-zа-яё]*", "Микас"),
    (r"\bm[\s\-_]*12(?:[\s\-_]*\.?[\s\-_]*\d+)?(?![0-9])|\bм[\s\-_]*12(?:[\s\-_]*\.?[\s\-_]*\d+)?(?![0-9])", "Микас 12"),
    # Bosch M1.5.4 / "бош 1.5.4" / папка "1.5.4 2111-1411020".
    (r"\bm[\s\-_]*1\.5\.4\b|\b(?:bosch|бош)[\s\-_]*1\.5\.4\b", "M1.5.4"),
    (r"\bsirius[\s\-_]*(?:d42|d52)\b|\bсириус[\s\-_]*(?:d42|d52)\b", "Sirius D42/D52"),
    (r"(?<![0-9a-zа-яё])sirius(?![0-9a-zа-яё])", "Sirius"),
    (r"\bme[\s\-_]*17[\s\-_]*\.?[\s\-_]*9[\s\-_]*\.?[\s\-_]*7\b|\bm17\.9\.7\b|\bм17\.9\.7\b", "ME17.9.7"),
    (r"\bmed[\s\-_]*17(?:[\s\-_]*\.?[\s\-_]*\d+)*\b", "MED17"),
    (r"\bme[\s\-_]*17(?:[\s\-_]*\.?[\s\-_]*\d+){1,2}\b", "ME17"),
    (r"\bme[\s\-_]*7(?:[\s\-_]*\.?[\s\-_]*\d+){1,2}\b|\bm7[\s\-_]*\.?[\s\-_]*9[\s\-_]*\.?[\s\-_]*7\b", "ME7.x"),
    (r"\bmed[\s\-_]*9[\s\-_]*\.?[\s\-_]*1\b", "MED9.1"),
    (r"\bmg1(?:cs|us|ua)?\b", "MG1"),
    (r"(?<![a-z0-9])edc[\s\-_]*(?:16|17)[a-z0-9._]*(?![a-z0-9])", "EDC16/EDC17"),
    (r"\bmev[\s\-_]*17(?:[\s\-_]*\.?[\s\-_]*\d+)*\b", "MEV17"),
    (r"\bsim[\s\-_]*2k[\s\-_]*(?:140|141|240|241|242|341)\b", "Sim2K-14x/24x/34x"),
    (r"\bsim[\s\-_]*2k\b", "Sim2K"),
    (r"\bsimk[\s\-_]*2k\b", "Sim2K"),
    (r"\bsimk[\s\-_]*\d{2}\b", "Sim2K"),
    (r"\bsimtec[\s\-_]*(?:75|76)\b", "Simtec 75/76"),
    (r"\bmt[\s\-_]*(?:20|22|38|60|62|80)u?\d?\b", "MT20U"),
    (r"\b(?:mp|мр)[\s\-_]*7(?:[\s\-_]*\.[\s\-_]*0)?[a-zа-яё]?\b", "MP7.0"),
    (r"\bdcm[\s\-_]*(?:3\.5|3\.6|6\.1|6\.2)\b", "DCM3.5/6.2"),
    (r"\bsid[\s\-_]*(?:310|803|804|807|305)\b", "SID"),
    (r"\bems[\s\-_]*(?:3132|3138)\b", "EMS 3132/3138"),
    (r"\biaw[\s\-_]*(?:1av|4|5|8|49|59|7gv|8f|10)\b", "IAW"),
    (r"\bias[\s\-_]*(?:7|8|9)\b", "IAW"),
    (r"\bmag(?:neti)?[\s\-_]*marelli\b|\bmm[\s\-_]*[0-9a-z]{4,}\b", "Magneti Marelli"),
    (r"\bkefico\b", "Kefico"),
    (r"\bdenso\b|\bденсо\b", "DENSO"),
    (r"\bkeihin\b|\bкейхин\b", "Keihin"),
    (r"\bjecs\b", "JECS"),
    (r"\bhitachi\b|\bхитачи\b", "Hitachi"),
    (r"\bcontinental\b|\bsiemens\b|\bсименс\b", "Continental/Siemens"),
    (r"\bабит\b|\babit\b", "Абит"),
]

# ---------------------------------------------------------------------------
# Регулярные выражения
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[^0-9a-zа-яё]+")
# Код блока ВАЗ/УАЗ: I484GKB2, I765LA02, A373CA02. Подчёркивание после кода
# (I484GKB2_Mat_Tun) не должно мешать — поэтому не \b, а явные границы.
_ECU_ID_RE = re.compile(
    r"(?<![0-9A-Za-zА-Яа-яЁё])[A-Z]\d{3}[A-Z0-9]{4}(?![0-9A-Za-zА-Яа-яЁё])"
)
# Калибровочный код ВАЗ по семейству: I444LR06, I414LX06, A317LR03, I427DB...
# Сами по себе такие коды означают Ладу, даже если марка нигде не написана.
# Калибровочный код: I444LR06, A317LR03, B574DB(LR)03, L646...
# Сами по себе такие коды означают Ладу, даже если марка нигде не написана.
_VAZ_CODE_RE = re.compile(
    r"(?<![0-9A-Z])([AIBL]\d{3}[A-Z]{2}[A-Z0-9]{0,3})(?![0-9A-Z])"
)
# Коды семейств V5/J5/M1 (Январь 5.1, M1.5.4) без дефисов и с дефисами.
_ALT_CODE_RE = re.compile(
    r"(?<![0-9A-Z])([VJM]\d[A-Z0-9]{5,7})(?![0-9A-Z])"
)

# Семейство кода блока -> (марка, ЭБУ). Таблица выведена из самой коллекции:
# взяты только семьи, где >=95% уже размеченных записей сходятся.
# Порядок важен: более длинные (точные) префиксы идут первыми.
_CODE_FAMILY_MAP: List[Tuple[str, str, Optional[str]]] = [
    ("A113", "Chery", "ME7.x"),
    ("B137", "BMW", "Continental/Siemens"),
    ("V5", "ВАЗ (LADA)", "Январь 5.1"),
    ("J5", "ВАЗ (LADA)", "Январь 5.1"),
    ("M1F", "ВАЗ (LADA)", "M1.5.4"),
    ("M1V", "ВАЗ (LADA)", "M1.5.4"),
    ("M1N", "ВАЗ (LADA)", "M1.5.4"),
    ("M7V", "ВАЗ (LADA)", "ME7.x"),
    ("M7N", "ВАЗ (LADA)", "ME7.x"),
    ("M7", "ВАЗ (LADA)", "ME7.x"),
    ("B515", "ВАЗ (LADA)", "ME17.9.7"),
    ("B516", "ВАЗ (LADA)", "ME17.9.7"),
    ("M124", "GAZ", "Микас 12"),
    ("I427", "ВАЗ (LADA)", "М74.5"),
    ("I674", "ВАЗ (LADA)", "М75"),
    ("I574", "ВАЗ (LADA)", "М75"),
    ("A201", "ВАЗ (LADA)", "Январь 7.2"),
    ("A203", "ВАЗ (LADA)", "Январь 7.2"),
    ("A204", "ВАЗ (LADA)", "Январь 7.2"),
    ("A205", "ВАЗ (LADA)", "Январь 7.2"),
    ("A302", "ВАЗ (LADA)", "М73"),
    ("A315", "ВАЗ (LADA)", "М73"),
    ("A316", "ВАЗ (LADA)", "М73"),
    ("A374", "ВАЗ (LADA)", "М73"),
    ("I207", "ВАЗ (LADA)", "Январь 7.2"),
    ("I226", "ВАЗ (LADA)", "Январь 7.2"),
    ("I233", "ВАЗ (LADA)", "Январь 7.2"),
    ("I316", "ВАЗ (LADA)", "М73"),
    ("I374", "ВАЗ (LADA)", "М73"),
    ("A2", "ВАЗ (LADA)", "Январь 7.2"),
    ("A3", "ВАЗ (LADA)", "М73"),
    ("B1", "ВАЗ (LADA)", None),
    ("B5", "ВАЗ (LADA)", "ME17.9.7"),
    ("I1", "ВАЗ (LADA)", None),
    ("I2", "ВАЗ (LADA)", "Январь 7.2"),
    ("I3", "ВАЗ (LADA)", "М73"),
    ("I4", "ВАЗ (LADA)", "М74"),
    ("I6", "ВАЗ (LADA)", "М74"),
    ("I7", "ВАЗ (LADA)", "М86"),
    ("I8", "ВАЗ (LADA)", "М74"),
    ("L6", "ВАЗ (LADA)", None),
    ("M1", "GAZ", None),
    ("M3", "Geely", None),
    ("Z0", "GAZ", "Микас 12"),
    ("C1", "BMW", "Continental/Siemens"),
    ("C2", "BMW", "Continental/Siemens"),
    ("C3", "BMW", "Continental/Siemens"),
    ("C4", "BMW", "Continental/Siemens"),
    ("C5", "BMW", "Continental/Siemens"),
    ("C6", "BMW", "Continental/Siemens"),
    ("G0", "Opel", "Continental/Siemens"),
    ("G3", "Opel", "Continental/Siemens"),
    ("Q4", "Opel", "Continental/Siemens"),
    ("S0", "Daewoo", "Sirius D42/D52"),
    ("S1", "Renault", "EMS 3132/3138"),
    ("E9", "Hyundai", "Kefico"),
    ("P020", "Derways", "MT20U"),
]

# Модель по папке внутри архива, когда марка уже известна.
_BRAND_MODEL_HINTS: List[Tuple[str, str, str]] = [
    ("Great Wall", "hover h2", "Hover H2"),
    ("Great Wall", "hover", "Hover"),
    ("Great Wall", "wingle", "Wingle"),
    ("JAC", "rein", "Rein"),
    ("Changan", "cs35", "CS35"),
    ("Changan", "cs55", "CS55"),
    ("Changan", "cs75", "CS75"),
    ("Brilliance", "m2", "M2"),
    ("Brilliance", "m3", "M3"),
    ("John Deere", "7210r", "7210R"),
    ("John Deere", "6155", "6155"),
    ("Derways", "aurora", "Aurora"),
]
_BOSCH_ID_RE = re.compile(r"(?<!\d)(0?261\d{6,8}|1037\d{6,8})(?!\d)")
_EURO_RE = re.compile(r"(?:евро|euro)[\s\-_]*([2-6])", re.IGNORECASE)
_EURO_SHORT_RE = re.compile(
    r"(?<![0-9a-zа-яё])[еe][\s\-_]*([2-6])(?![0-9a-zа-яё])", re.IGNORECASE
)
_STAGE_RE = re.compile(
    r"(?<![0-9a-zа-яё])(?:stage|st|ст|стейдж)[\s\-_]*([1-3])(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-2]\d)(?!\d)")
_DISPLACEMENT_RE = re.compile(r"(?<![\d.])([12]\.[0-9])(?![\d.])")
_VALVES_RE = re.compile(
    r"(?<!\d)(8|12|16|20|24)[\s\-_]*(?:v|кл\.?|кл\b|valve)", re.IGNORECASE
)
_FUEL_RE = re.compile(
    r"(?<![0-9a-zа-яё])(tdi|dci|hdi|cdi|cdti|crdi|tsi|tfsi|mpi|vvt|gdi|d-4d|d4d)(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_TRANSMISSION_RE = re.compile(
    r"(?<![0-9a-zа-яё])(акпп|cvt|вариатор|dsg|s[\s\-_]*tronic|робот|мкпп|amt|mt)(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_AUTHOR_RE = re.compile(
    r"(?<![0-9a-zа-яё])(?:автор|author|by|от)(?![0-9a-zа-яё])"
    r"\s*[:\-]?\s*([A-Za-zА-Яа-яЁё][\w .\-]{1,30})",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"[\[\(]([^\]\)]{2,40})[\]\)]")
# Подсказки пароля в именах архивов: \"pass 4pda\", \"пароль 12345\".
# Их нельзя считать автором/моделью/годом прошивки.
_PASSWORD_HINT_RE = re.compile(
    r"(?:пароль|password|pass|pw)(?![a-zа-яё])\s*[:\-:=]?\s*"
    r"(?:(?:пароль|password|pass|pw)(?![a-zа-яё])\s*[:\-:=]?\s*)?"
    r"[^\s()\[\]{}]+",
    re.IGNORECASE,
)
_VAZ_NUM_RE = re.compile(r"(?<!\d)(21[0-9]{2}|217[0-2]|219[0-5]|111[1-9])(?!\d)")
_VERSION_TAG_RE = re.compile(
    r"(?<![0-9a-zа-яё])(max|l5|upd\d+|mr\d+|cvn|startpower|v\d+(?:\.\d+)?|rev\d+)(?![0-9a-zа-яё])",
    re.IGNORECASE,
)

_KNOWN_AUTHORS = [
    "паулюс", "paulus", "адакт", "adact", "стоянов", "армеев", "ледокол",
    "мотор-мастер", "motor-master", "motor master", "sms", "chiptuning",
    "sct", "evo", "wolf", "klaus", "abritus", "абитус", "combiloader",
    "комбилоадер", "sms-soft", "sms soft", "vitaly", "виталий",
]

_TUNE_RE = re.compile(
    r"(?<![0-9a-zа-яё])("
    r"tun(?:e|ed|ing)?|stage|st[1-3]|sport|race|chip|чип|тюн\w*|"
    r"mod\d*|"
    r"egr[\s\-_]*(?:off|delete|del)|егр[\s\-_]*(?:off|удал\w*|отключ\w*)|"
    r"dpf[\s\-_]*(?:off|delete|del)|сажев\w*[\s\-_]*(?:удал|отключ)|"
    r"adblue[\s\-_]*(?:off|delete|emul\w*)|v[\s\-_]*max[\s\-_]*(?:off|delete)|"
    r"pops|bangs|crackle|burble|launch|map"
    r")(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_STOCK_RE = re.compile(
    r"(?<![0-9a-zа-яё])(stock|factory|series|original|orig|сток|серийн\w*|оригинал\w*)(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_IMMO_RE = re.compile(
    r"(?<![0-9a-zа-яё])(imm[\s\-_]*off|immo[\s\-_]*off|noimm|иммо[\s\-_]*(?:off|откл\w*|удал\w*))(?![0-9a-zа-яё])",
    re.IGNORECASE,
)
_GBO_RE = re.compile(
    r"(?<![0-9a-zа-яё])(гбо|hbo|lpg|cng|метан|пропан|газобаллон\w*)(?![0-9a-zа-яё])",
    re.IGNORECASE,
)

_EXTRA_TAG_RULES: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"egr[\s\-_]*(?:off|delete|del)|егр[\s\-_]*(?:off|удал\w*|отключ\w*)", re.I), "EGR off"),
    (re.compile(r"dpf[\s\-_]*(?:off|delete|del)|сажев\w*[\s\-_]*(?:удал|отключ)", re.I), "DPF off"),
    (re.compile(r"adblue[\s\-_]*(?:off|delete|emul\w*)", re.I), "AdBlue off"),
    (re.compile(r"v[\s\-_]*max[\s\-_]*(?:off|delete)", re.I), "Vmax off"),
    (re.compile(r"(?:pops|bangs|crackle|burble)", re.I), "Pops"),
    (re.compile(r"launch", re.I), "Launch"),
]

_ECU_COMPILED = [(re.compile(pattern, re.IGNORECASE), name) for pattern, name in ECU_RULES]

_BRAND_INDEX: Dict[Tuple[str, ...], str] = {}
for _brand, _aliases in BRAND_ALIASES.items():
    for _alias in _aliases:
        _tokens = tuple(_SPLIT_RE.split(_alias.lower()))
        _tokens = tuple(token for token in _tokens if token)
        if _tokens:
            _BRAND_INDEX[_tokens] = _brand

_MODEL_INDEX: Dict[Tuple[str, ...], str] = {}
for _model, _aliases in MODEL_ALIASES.items():
    for _alias in _aliases:
        _tokens = tuple(_SPLIT_RE.split(_alias.lower()))
        _tokens = tuple(token for token in _tokens if token)
        if _tokens:
            _MODEL_INDEX[_tokens] = _model

_MODEL_TO_BRAND: dict[str, str] = {}
for _brand, _aliases in BRAND_ALIASES.items():
    for _alias in _aliases:
        _tokens = tuple(token for token in _SPLIT_RE.split(_alias.lower()) if token)
        if _tokens and _tokens in _MODEL_INDEX:
            _MODEL_TO_BRAND.setdefault(_MODEL_INDEX[_tokens], _brand)

_BRAND_NAME_INDEX: dict[tuple[str, ...], str] = {}
for _brand, _aliases in BRAND_ALIASES.items():
    for _alias in _aliases:
        _tokens = tuple(token for token in _SPLIT_RE.split(_alias.lower()) if token)
        if _tokens and _tokens not in _MODEL_INDEX:
            _BRAND_NAME_INDEX[_tokens] = _brand

_MAX_ALIAS_TOKENS = max(
    [len(key) for key in _BRAND_INDEX] + [len(key) for key in _MODEL_INDEX] + [1]
)


def _tokenize(text: str) -> List[str]:
    return [token for token in _SPLIT_RE.split(text.lower()) if token]


# Кириллические буквы-двойники латинских: в именах встречается "Cruzе" (с русской "е"),
# "kитай", "Mersedes". Слова, где смешаны обе азбуки, приводим к латинице.
_CYR_TO_LAT = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "к": "k", "м": "m", "т": "t", "в": "b", "н": "h", "і": "i", "ѕ": "s",
    "А": "A", "Е": "E", "О": "O", "Р": "P", "С": "C", "У": "Y", "Х": "X",
    "К": "K", "М": "M", "Т": "T", "В": "B", "Н": "H", "І": "I",
}
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІі]+")


def _normalize_homoglyphs(text: str) -> str:
    """Приводит смешанные слова к латинице: "Cruzе" -> "Cruze"."""
    def fix(match: "re.Match[str]") -> str:
        word = match.group(0)
        has_latin = any("a" <= ch.lower() <= "z" for ch in word)
        has_cyrillic = any("\u0400" <= ch <= "\u04FF" for ch in word)
        if has_latin and has_cyrillic:
            return "".join(_CYR_TO_LAT.get(ch, ch) for ch in word)
        return word

    return _WORD_RE.sub(fix, text)


def _find_alias(tokens: List[str], index: Dict[Tuple[str, ...], str]) -> Optional[str]:
    """Ищет самое длинное совпадение по словарю алиасов (быстро, без regex)."""
    best: Optional[str] = None
    best_length = 0
    count = len(tokens)
    for size in range(min(_MAX_ALIAS_TOKENS, count), 0, -1):
        for start in range(0, count - size + 1):
            key = tuple(tokens[start:start + size])
            value = index.get(key)
            if value is None and size == 1:
                # "Golf4", "Camry2010" -> "golf", "camry"
                stripped = key[0].rstrip("0123456789")
                if stripped and stripped != key[0]:
                    value = index.get((stripped,))
            if value and size > best_length:
                best = value
                best_length = size
    return best


def _basename(name: str) -> str:
    return str(name).replace("\\", "/").rsplit("/", 1)[-1]


def _strip_ext(name: str) -> str:
    base = _basename(name)
    index = base.rfind(".")
    return base[:index] if index > 0 else base


# ---------------------------------------------------------------------------
# Парсер имён
# ---------------------------------------------------------------------------


class FirmwareNameParser:
    """Извлекает метаданные из пути и имени файла прошивки."""

    def parse(self, full_path: str, root_dir: Optional[str] = None) -> FirmwareMeta:
        """Разбирает имя файла (и, при необходимости, путь до него)."""
        chain = [part for part in str(full_path).split("::") if part != ""]
        physical = chain[0] if chain else str(full_path)

        if len(chain) > 1:
            member = chain[-1]
            filename = _basename(member)
            # Путь внутри архива — главный источник данных, когда в имени
            # файла только код: "Tazoff 15.01.26/06.2025/ВАЗ/M86/.../1.6 16V/".
            # Раньше брались две последние папки, и марка/ЭБУ терялись.
            inner_parts = [part for part in member.split("/")[:-1] if part]
            # Промежуточные звенья цепочки тоже несут путь:
            # "...rar::Tazoff/ГАЗ/Газель/Iron/M114.rar::…/файл.bin".
            archive_parts: List[str] = []
            for link in chain[1:-1]:
                archive_parts.extend(
                    part for part in str(link).replace("\\", "/").split("/") if part
                )
            # Папки НАД архивом тоже важны: "...\китай_LDKL\797\Geely\VISION.rar".
            # Раньше от физического пути оставалось только имя архива.
            physical_dirs = [
                part
                for part in physical.replace("/", "\\").split("\\")[:-1]
                if part
            ]
            context_parts = (
                [_strip_ext(physical)]
                + physical_dirs[-3:]
                + archive_parts
                + inner_parts
                + [filename]
            )
        else:
            filename = os.path.basename(physical)
            directory = os.path.dirname(physical)
            if root_dir:
                try:
                    relative = os.path.relpath(directory, root_dir)
                except ValueError:
                    relative = directory
            else:
                relative = directory
            parts = [] if relative in (".", "") else relative.split(os.sep)
            context_parts = parts[-12:] + [filename]

        # Сначала контекст пути/архива: там обычно более точная марка,
        # модель и версия ЭБУ (например, M74.9, а не общий M74 из кода файла).
        meta = self.parse_text(" ".join(context_parts))
        filename_meta = self.parse_text(_strip_ext(filename))
        meta.fill_missing(filename_meta)
        return meta

    def parse_text(self, text: str) -> FirmwareMeta:
        """Разбирает произвольную строку (имя файла, строка описания)."""
        meta = FirmwareMeta()
        if not text:
            return meta

        # Убираем \"pass ...\"/\"пароль ...\" из имён архивов, иначе они
        # попадают в автора, год и другие поля.
        text = _PASSWORD_HINT_RE.sub(" ", text)
        # "Cruzе" (с русской «е»), "kитай" — слова со смешанными азбуками.
        text = _normalize_homoglyphs(text)

        raw = text.lower()
        tokens = _tokenize(text)

        brand = _find_alias(tokens, _BRAND_NAME_INDEX)
        if brand:
            meta.brand = brand

        # Код блока подсказывает марку и ЭБУ: "I444LR06", "B574DB03", "P0204AXZ".
        self._apply_code_family(meta, text)

        model = _find_alias(tokens, _MODEL_INDEX)
        if not brand and model:
            inferred_brand = _MODEL_TO_BRAND.get(model)
            if inferred_brand:
                meta.brand = inferred_brand

        if not model and brand == "ВАЗ (LADA)":
            match = _VAZ_NUM_RE.search(" ".join(tokens))
            if match:
                model = match.group(1)
        if model:
            meta.model = model

        # Модель из папки, когда марка уже известна: "Great Wall Hover H2(MOD8)".
        if meta.brand and not meta.model:
            joined = " ".join(tokens)
            for hint_brand, phrase, hint_model in _BRAND_MODEL_HINTS:
                if meta.brand == hint_brand and phrase in joined:
                    meta.model = hint_model
                    break

        # Подчёркивания в кодах ("EDC16_15.06", "M1.5.4_GBO") мешают \b.
        ecu_raw = raw.replace("_", " ")
        for pattern, name in _ECU_COMPILED:
            if pattern.search(ecu_raw):
                meta.ecu_name = name
                break

        # "Микас11ЕТ" -> "Микас 11", "Микас 12" и т.п.
        if meta.ecu_name == "Микас":
            number = re.search(r"(?:mikas|микас)[\s\-_]*(\d+)", raw)
            if number:
                meta.ecu_name = f"Микас {number.group(1)}"

        ecu_id = _ECU_ID_RE.search(text.upper())
        if ecu_id:
            meta.ecu_id = ecu_id.group(0)
        else:
            bosch = _BOSCH_ID_RE.search(text)
            if bosch:
                meta.ecu_id = bosch.group(1)

        eco = _EURO_RE.search(raw)
        if not eco:
            eco = _EURO_SHORT_RE.search(raw)
        if eco:
            meta.eco_standard = "E" + eco.group(1)

        meta.has_gbo = bool(_GBO_RE.search(raw))
        meta.is_tune = bool(_TUNE_RE.search(raw))
        meta.is_stock = bool(_STOCK_RE.search(raw))
        meta.imm_off = bool(_IMMO_RE.search(raw))

        stage = _STAGE_RE.search(raw)
        if stage:
            meta.extra_tags.append(f"Stage {stage.group(1)}")
            meta.is_tune = True

        for pattern, tag in _EXTRA_TAG_RULES:
            if pattern.search(raw) and tag not in meta.extra_tags:
                meta.extra_tags.append(tag)

        transmission = _TRANSMISSION_RE.search(raw)
        if transmission:
            meta.extra_tags.append(transmission.group(1).upper())

        version_tags = []
        for match in _VERSION_TAG_RE.finditer(text):
            value = match.group(1)
            canonical = value.upper() if value.lower() in ("max", "l5", "cvn") else value
            if value.lower() == "startpower":
                canonical = "Startpower"
            if canonical.lower() not in (tag.lower() for tag in version_tags):
                version_tags.append(canonical)
        if version_tags:
            meta.version_tag = version_tags[0]
            for tag in version_tags[1:]:
                if tag not in meta.extra_tags:
                    meta.extra_tags.append(tag)

        author = self._detect_author(text)
        if author:
            meta.author = author

        engine = self._detect_engine(raw)
        if engine:
            meta.engine = engine

        year = _YEAR_RE.search(raw)
        if year:
            meta.year = int(year.group(1))

        return meta

    @staticmethod
    def _apply_code_family(meta: FirmwareMeta, text: str) -> None:
        """Марка и ЭБУ по семейству кода блока (I444LR06, B574DB03, P0204AXZ)."""
        upper = "".join(_CYR_TO_LAT.get(ch, ch) for ch in text).upper()
        match = (
            _ECU_ID_RE.search(upper)
            or _VAZ_CODE_RE.search(upper)
            or _ALT_CODE_RE.search(upper)
        )
        if not match:
            return
        family = match.group(0)[:4].upper()
        for prefix, family_brand, family_ecu in _CODE_FAMILY_MAP:
            if family.startswith(prefix):
                if not meta.brand:
                    meta.brand = family_brand
                if not meta.ecu_name and family_ecu:
                    meta.ecu_name = family_ecu
                return

    @staticmethod
    def _detect_author(text: str) -> Optional[str]:
        lowered = text.lower()
        for known in _KNOWN_AUTHORS:
            if re.search(
                r"(?<![0-9a-zа-яё])" + re.escape(known) + r"(?![0-9a-zа-яё])",
                lowered,
            ):
                return known.title() if known.isascii() else known.capitalize()

        match = _AUTHOR_RE.search(text)
        if match:
            candidate = " ".join(match.group(1).split()[:3]).strip(" .-")
            if 2 <= len(candidate) <= 32 and re.search(r"[A-Za-zА-Яа-яЁё]{2,}", candidate):
                return candidate

        for match in _BRACKET_RE.finditer(text):
            candidate = match.group(1).strip(" .-")
            if not (2 <= len(candidate) <= 32):
                continue
            if len(candidate.split()) > 3:
                continue
            if not re.search(r"[A-Za-zА-Яа-яЁё]{2,}", candidate):
                continue
            if re.search(r"\d{3,}|евро|euro|stage|гбо|ecu|эбу", candidate, re.I):
                continue
            return candidate

        return None

    @staticmethod
    def _detect_engine(raw: str) -> Optional[str]:
        parts: List[str] = []
        displacement = _DISPLACEMENT_RE.search(raw)
        if displacement:
            parts.append(displacement.group(1))
        valves = _VALVES_RE.search(raw)
        if valves:
            parts.append(valves.group(1) + "V")
        fuel = _FUEL_RE.search(raw)
        if fuel:
            parts.append(fuel.group(1).upper())
        return " ".join(parts) if parts else None


# ---------------------------------------------------------------------------
# Парсер текстовых описаний
# ---------------------------------------------------------------------------


class TextDescriptionParser:
    """Сопоставляет содержимое .txt/.readme/.nfo с файлами прошивок."""

    def __init__(self, name_parser: Optional[FirmwareNameParser] = None) -> None:
        self.name_parser = name_parser or FirmwareNameParser()

    def parse(
        self, text: str, firmware_names: Iterable[str]
    ) -> Dict[str, Dict[str, object]]:
        """Возвращает {имя_прошивки: {метаданные}} по тексту описания."""
        names = [str(name) for name in firmware_names]
        if not text or not names:
            return {}

        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lowered = [line.lower() for line in lines]
        result: Dict[str, Dict[str, object]] = {}

        for name in names:
            base = _basename(name).lower()
            stem = _strip_ext(name).lower()
            indexes: List[int] = []
            for variant in (name.lower(), base, stem):
                if len(variant) < 3:
                    continue
                indexes = [i for i, line in enumerate(lowered) if variant in line]
                if indexes:
                    break
            if not indexes:
                continue

            context_lines: List[str] = []
            for index in indexes[:3]:
                context_lines.extend(lines[max(0, index - 1):index + 3])
            meta = self.name_parser.parse_text("\n".join(context_lines)).to_dict()
            if meta:
                result[name] = meta

        if not result and len(names) <= 3:
            meta = self.name_parser.parse_text(text).to_dict()
            if meta:
                for name in names:
                    result[name] = dict(meta)

        return result
