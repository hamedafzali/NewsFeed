"""Grammar notes for German lessons, written once and reviewed by a person.

The lesson writer only chooses a topic and points to a sentence in its text; it never
writes the explanation, so a model mistake can't teach a wrong rule. Each topic has
`markers`: patterns the example sentence must match, to catch a sentence that doesn't
show the topic at all.
"""
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

I = re.IGNORECASE


@dataclass(frozen=True)
class GrammarTopic:
    id: str
    name: str            # shown in the post, in German
    explanation_fa: str
    sample: str          # a sentence showing the topic (used by tests)
    markers: Tuple[re.Pattern, ...] = ()

    def shown_by(self, sentence: str) -> bool:
        return all(m.search(sentence) for m in self.markers)


def _p(*patterns: str, flags: int = I) -> Tuple[re.Pattern, ...]:
    return tuple(re.compile(p, flags) for p in patterns)


# A Partizip II: lowercase (nouns are capitalised), ge-/inseparable-prefix/-iert forms,
# minus common words that only look like one.
PARTICIPLE = re.compile(
    r"\b(?!(gestern|gegen|genug|genau|gerade|gern|gesamt|beiden|bereit|bereits|erst|ersten|bisher|vergangen)\b)"
    r"(ge[a-zäöüß]{2,}[tn]|[a-zäöüß]{2,}iert|(be|er|ver|ent|zer|emp|miss)[a-zäöüß]{2,}[tn]"
    r"|(an|auf|aus|ein|mit|vor|zurück|ab|fest|teil|weg|zu|statt|bei|nach|fort|her|hin|los)ge[a-zäöüß]{2,}[tn])\b"
)

# Perfekt puts the participle at the end of the clause ("hat das Gesetz beschlossen."),
# unlike an adjective after sein ("ist bekannt für ..."); adjectives that look like
# participles are left out.
CLAUSE_END_PARTICIPLE = re.compile(
    r"\b(?!(bekannt|verwandt|beliebt|berühmt|beschäftigt|besorgt|entspannt|erlaubt|verboten|gesund|"
    r"gestern|gegen|genug|genau|gerade|gern|gesamt|beiden|bereit|bereits|erst|ersten|bisher|vergangen)\b)"
    r"(ge[a-zäöüß]{2,}[tn]|[a-zäöüß]{2,}iert|(be|er|ver|ent|zer|emp|miss)[a-zäöüß]{2,}[tn]"
    r"|(an|auf|aus|ein|mit|vor|zurück|ab|fest|teil|weg|zu|statt|bei|nach|fort|her|hin|los)ge[a-zäöüß]{2,}[tn])"
    r"\s*([.,;:!?“”\"»«)–-]|$)"
)

GERMAN_TOPICS: List[GrammarTopic] = [
    GrammarTopic(
        "perfekt", "Perfekt",
        "Perfekt رایج‌ترین زمان گذشته در گفتار آلمانی است: فعل کمکی haben یا sein صرف می‌شود و در جایگاه دوم "
        "جمله می‌آید و اسم مفعول (Partizip II) به آخر جمله می‌رود. بیشتر فعل‌ها با haben می‌آیند؛ فعل‌هایی که "
        "حرکت یا تغییر حالت را نشان می‌دهند (مثل gehen، fahren، sterben) و همچنین sein و bleiben با sein.",
        "Die Regierung hat das Gesetz beschlossen.",
        _p(r"\b(habe|hast|hat|haben|habt|bin|bist|ist|sind|seid)\b") + (CLAUSE_END_PARTICIPLE,),
    ),
    GrammarTopic(
        "praeteritum", "Präteritum",
        "در اخبار و متن‌های نوشتاری، گذشته بیشتر با Präteritum بیان می‌شود؛ به‌ویژه برای sein (war)، haben (hatte) "
        "و افعال وجهی (konnte، musste). فعل‌های باقاعده پسوند ‎-te‎ می‌گیرند (sagte، machte) و فعل‌های بی‌قاعده "
        "ریشه‌شان تغییر می‌کند (kam، ging، gab).",
        "Im Sommer war es sehr heiß.",
        _p(r"\b(war|waren|hatte|hatten|konnte|konnten|musste|mussten|wollte|wollten|durfte|durften|gab|gaben|kam|kamen|"
           r"ging|gingen|sagte|sagten|stand|standen|fand|fanden|nahm|nahmen|blieb|blieben|starb|starben|fuhr|fuhren|"
           r"lag|lagen|hieß|zeigte|zeigten|forderte|forderten|erklärte|erklärten|betonte|meldete|lebte|lebten)\b"),
    ),
    GrammarTopic(
        "passiv", "Passiv",
        "وقتی کننده‌ی کار مهم یا معلوم نیست، از جمله‌ی مجهول (Passiv) استفاده می‌شود: werden صرف می‌شود و اسم "
        "مفعول (Partizip II) به آخر جمله می‌رود: «Das Gesetz wird beschlossen.» در گذشته wurde/wurden می‌آید و در "
        "Perfekt «ist … worden».",
        "Die Straße wurde gesperrt.",
        _p(r"\b(wird|werden|wurde|wurden|worden)\b") + (PARTICIPLE,),
    ),
    GrammarTopic(
        "modalverben", "Modalverben",
        "افعال وجهی (können، müssen، wollen، sollen، dürfen) معنای «توانستن، مجبور بودن، خواستن، قرار بودن، "
        "اجازه داشتن» را به جمله اضافه می‌کنند. فعل وجهی صرف می‌شود و در جایگاه دوم می‌آید و فعل اصلی به شکل "
        "مصدر به آخر جمله می‌رود: «Die Stadt muss mehr Wohnungen bauen.»",
        "Viele Menschen können die Miete nicht mehr bezahlen.",
        _p(r"\b(kann|kannst|können|könnt|muss|musst|müssen|müsst|will|willst|wollen|wollt|soll|sollst|sollen|sollt|"
           r"darf|darfst|dürfen|dürft|konnte|konnten|musste|mussten|wollte|wollten|sollte|sollten|durfte|durften)\b"),
    ),
    GrammarTopic(
        "dass", "Nebensatz mit „dass“",
        "جمله‌ای که با dass («که») شروع می‌شود جمله‌ی وابسته است و فعل صرف‌شده در آن به آخر جمله می‌رود. "
        "این ساختار در اخبار برای نقل گفته‌ها بسیار رایج است: «Sie sagt, dass die Preise steigen.»",
        "Experten sagen, dass die Preise weiter steigen.",
        _p(r"\bdass\b"),
    ),
    GrammarTopic(
        "weil", "Nebensatz mit „weil“",
        "weil («چون، زیرا») علت را بیان می‌کند و فعل صرف‌شده را به آخر جمله می‌فرستد: «…, weil die Preise "
        "gestiegen sind.» کلمه‌ی denn هم «چون» معنی می‌دهد، اما ترتیب کلمات را تغییر نمی‌دهد.",
        "Die Schulen bleiben geschlossen, weil es zu viel Schnee gibt.",
        _p(r"\bweil\b"),
    ),
    GrammarTopic(
        "als_wenn", "Temporalsätze mit „als“ und „wenn“",
        "als برای یک رویداد یک‌باره در گذشته به کار می‌رود (Als der Zug ankam, …) و wenn برای حال و آینده یا "
        "برای کارهایی که بارها تکرار شده‌اند. در هر دو، فعل صرف‌شده به آخر جمله‌ی وابسته می‌رود.",
        "Als die Polizei kam, waren die Täter schon weg.",
        _p(r"\b(als|wenn)\b[^.]*\b\w+\s*,|,\s*(als|wenn)\b"),
    ),
    GrammarTopic(
        "obwohl", "Nebensatz mit „obwohl“",
        "obwohl («با اینکه، اگرچه») تضاد را نشان می‌دهد و فعل صرف‌شده را به آخر جمله می‌برد: «Obwohl es "
        "regnet, demonstrieren viele Menschen.» همین معنا را می‌توان با trotzdem («با این حال») در جمله‌ی اصلی "
        "بیان کرد.",
        "Obwohl es regnete, kamen tausende Menschen.",
        _p(r"\b(obwohl|trotzdem)\b"),
    ),
    GrammarTopic(
        "relativsatz", "Relativsatz",
        "جمله‌ی موصولی یک اسم را توضیح می‌دهد و با ضمیر موصولی (der، die، das، den، dem، deren …) شروع می‌شود. "
        "جنس و تعداد ضمیر از همان اسم می‌آید و حالت (Kasus) آن از نقشش در جمله‌ی موصولی؛ فعل صرف‌شده به آخر "
        "جمله‌ی موصولی می‌رود.",
        "Der Mann, der den Preis bekommen hat, lebt in Berlin.",
        _p(r",\s*(\w+\s+)?(der|die|das|den|dem|denen|deren|dessen)\s"),
    ),
    GrammarTopic(
        "indirekte_frage", "Indirekte Fragen",
        "پرسش غیرمستقیم با ob («آیا») یا یک کلمه‌ی پرسشی (wie، warum، wann، wo، was، wer) شروع می‌شود و فعل "
        "صرف‌شده به آخر جمله می‌رود: «Es ist unklar, ob die Regierung zustimmt.»",
        "Noch ist unklar, wann die Arbeiten beginnen.",
        _p(r",\s*(ob|wie|warum|wann|wo|was|wer|wieso|weshalb|wohin|woher|welche\w*)\b"),
    ),
    GrammarTopic(
        "zu_infinitiv", "Infinitiv mit „zu“",
        "بعد از برخی فعل‌ها و عبارت‌ها (versuchen، planen، beginnen، es ist wichtig …) فعل دوم به شکل «zu + مصدر» "
        "در آخر جمله می‌آید. در فعل‌های جداشدنی، zu بین پیشوند و فعل قرار می‌گیرد: anzufangen، einzuführen.",
        "Die Stadt plant, mehr Bäume zu pflanzen.",
        _p(r"\bzu\s+\w+(en|ern|eln)\b|\b(an|auf|aus|ein|mit|vor|zurück|ab|fest|teil|weg|statt|bei|nach)zu\w+en\b"),
    ),
    GrammarTopic(
        "um_zu_damit", "Finalsätze: „um … zu“ und „damit“",
        "برای بیان هدف («برای اینکه») از «um … zu + مصدر» استفاده می‌شود وقتی فاعل هر دو بخش جمله یکی است، و از "
        "damit وقتی فاعل‌ها فرق دارند. در هر دو حالت، فعل به آخر جمله می‌رود.",
        "Die Regierung gibt Geld, um Familien zu helfen.",
        _p(r"\bum\b[^.]*\bzu\b|,\s*damit\b"),
    ),
    GrammarTopic(
        "akkusativ_praep", "Präpositionen mit Akkusativ",
        "حروف اضافه‌ی für، gegen، ohne، durch، um و bis همیشه با حالت مفعولی (Akkusativ) می‌آیند: für den Frieden، "
        "gegen die Regierung. در Akkusativ فقط حرف تعریف مذکر تغییر می‌کند: der → den و ein → einen.",
        "Tausende demonstrieren gegen den Krieg.",
        _p(r"\b(für|gegen|ohne|durch)\b"),
    ),
    GrammarTopic(
        "dativ_praep", "Präpositionen mit Dativ",
        "حروف اضافه‌ی mit، nach، bei، seit، von، zu، aus و gegenüber همیشه با حالت Dativ می‌آیند: mit dem "
        "Präsidenten، seit einem Jahr. در Dativ حرف تعریف der و das به dem، die به der، و جمع به den تبدیل "
        "می‌شود (و اسم جمع معمولاً ‎-n‎ می‌گیرد).",
        "Seit einem Jahr wartet die Familie auf eine Wohnung.",
        _p(r"\b(mit|nach|bei|beim|seit|von|vom|zum|zur|aus|gegenüber)\b|\bzu\b(?!\s+[a-zäöüß]+(en|ern|eln)\b)"),
    ),
    GrammarTopic(
        "wechselpraep", "Wechselpräpositionen",
        "حروف اضافه‌ی in، an، auf، über، unter، vor، hinter، neben و zwischen دو حالت دارند: برای مکان (پرسش «Wo?» "
        "= کجا؟) با Dativ می‌آیند و برای جهت حرکت (پرسش «Wohin?» = به کجا؟) با Akkusativ: in der Stadt (در شهر)، "
        "اما in die Stadt (به شهر).",
        "Die Menschen gingen auf die Straße.",
        _p(r"\b(in|im|ins|an|am|ans|auf|über|unter|hinter|neben|zwischen)\b"),
    ),
    GrammarTopic(
        "genitiv", "Genitiv",
        "حالت Genitiv تعلق را نشان می‌دهد، شبیه کسره‌ی اضافه در فارسی: der Präsident des Landes (رئیس‌جمهورِ "
        "کشور). حرف تعریف مذکر و خنثی des می‌شود و اسم ‎-s‎ یا ‎-es‎ می‌گیرد؛ مؤنث و جمع der می‌گیرند. حروف اضافه‌ی "
        "wegen، trotz و während هم در زبان نوشتاری با Genitiv می‌آیند.",
        "Der Sohn des letzten Königs lebt im Ausland.",
        _p(r"\b(des|eines|wegen|trotz|während)\b"),
    ),
    GrammarTopic(
        "trennbare_verben", "Trennbare Verben",
        "در فعل‌های جداشدنی (ankommen، teilnehmen، vorstellen …) پیشوند در جمله‌ی اصلی جدا می‌شود و به آخر جمله "
        "می‌رود: «Die Ministerin stellt den Plan vor.» در Partizip II، ‎-ge-‎ بین پیشوند و فعل می‌آید: angekommen، "
        "teilgenommen.",
        "Der Zug kommt heute später an.",
        _p(r"\b(an|auf|aus|ein|mit|vor|zurück|ab|fest|teil|weg|zu|statt|bei|her|hin|los|nach|fort)[\s.!?\"“”»«]*$"
           r"|\b(an|auf|aus|ein|mit|vor|zurück|ab|fest|teil|weg|zu|statt|bei|nach|fort)ge\w+[tn]\b"),
    ),
    GrammarTopic(
        "reflexive_verben", "Reflexive Verben",
        "فعل‌های بازتابی با ضمیر انعکاسی می‌آیند (mich، dich، sich، uns، euch): sich treffen (ملاقات کردن)، "
        "sich entscheiden (تصمیم گرفتن)، sich beschweren (شکایت کردن). برای سوم‌شخص مفرد و جمع، ضمیر همیشه sich است.",
        "Die Politiker treffen sich am Montag.",
        _p(r"\b(sich|mich|dich|uns|euch)\b"),
    ),
    GrammarTopic(
        "konjunktiv1", "Indirekte Rede (Konjunktiv I)",
        "در اخبار، گفته‌های دیگران اغلب با Konjunktiv I نقل می‌شود تا نشان دهد خبرنگار فقط گفته را بازگو می‌کند: "
        "«Der Minister sagte, die Lage sei ernst.» رایج‌ترین شکل‌ها sei (از sein)، habe (از haben) و werde "
        "(از werden) هستند.",
        "Die Polizei sagte, die Lage sei ruhig.",
        _p(r"\b(sei|seien|habe|werde|könne|müsse|solle|wolle|gebe|dürfe)\b"),
    ),
    GrammarTopic(
        "konjunktiv2", "Konjunktiv II",
        "Konjunktiv II برای حالت‌های فرضی، شرط، آرزو و درخواست مؤدبانه به کار می‌رود: würde + مصدر، wäre (از sein) "
        "و hätte (از haben). در اخبار برای بیان احتمال زیاد دیده می‌شود: «Das würde viele Menschen betreffen.»",
        "Ohne Hilfe wäre die Lage noch schlimmer.",
        _p(r"\b(würde|würden|wäre|wären|hätte|hätten|könnte|könnten|müsste|müssten)\b"),
    ),
    GrammarTopic(
        "futur", "Futur I",
        "برای آینده‌ی برنامه‌ریزی‌شده یا پیش‌بینی از werden + مصدر استفاده می‌شود؛ werden صرف می‌شود و مصدر به آخر "
        "جمله می‌رود: «Die Preise werden steigen.» در گفتار اغلب زمان حال همراه با یک قید زمان کافی است: "
        "«Morgen beginnt der Streik.»",
        "Im Winter werden die Preise weiter steigen.",
        _p(r"\b(wird|werden|werde|wirst)\b"),
    ),
    GrammarTopic(
        "komparativ", "Komparativ und Superlativ",
        "صفت تفضیلی با ‎-er‎ و «als» ساخته می‌شود (höher als، mehr als) و صفت عالی با «am …-sten» یا «der/die/das "
        "…-ste» (am höchsten، der größte). برخی صفت‌های کوتاه umlaut می‌گیرند: groß → größer، alt → älter.",
        "Die Mieten sind höher als im letzten Jahr.",
        _p(r"\b\w+er\s+als\b|\b(mehr|weniger|besser|lieber)\s+als\b|\bam\s+\w+sten\b|\b\w+(st|ßt)e[nmrs]?\b"),
    ),
    GrammarTopic(
        "adjektivendungen", "Adjektivendungen",
        "صفتی که قبل از اسم می‌آید پسوند می‌گیرد. بعد از der، die و das پسوند معمولاً ‎-e‎ یا ‎-en‎ است (der neue Plan، "
        "die neuen Regeln)؛ بعد از ein بدون پسوند یا وقتی حرف تعریف نیست، خودِ صفت جنس و حالت را نشان می‌دهد "
        "(ein neuer Plan، neues Geld).",
        "Die neue Regierung hat einen großen Plan.",
        _p(r"\b([Dd](er|ie|as|en|em|es)|[Ee]in(e|en|em|er|es)?)\s+[a-zäöüß]\w*(e|en|er|es|em)\s+[A-ZÄÖÜ]\w+", flags=0),
    ),
    GrammarTopic(
        "verneinung", "Verneinung: „nicht“ und „kein“",
        "kein برای منفی کردن اسم‌هایی است که بدون حرف تعریف یا با ein می‌آیند (Es gibt keine Lösung) و مثل ein صرف "
        "می‌شود. nicht فعل، صفت، قید یا اسمِ معرفه را منفی می‌کند و معمولاً قبل از بخشی که منفی می‌شود یا نزدیک آخر "
        "جمله می‌آید.",
        "Die Regierung hat noch keine Lösung.",
        _p(r"\b(nicht|kein|keine|keinen|keinem|keiner|keines)\b"),
    ),
    GrammarTopic(
        "verbzweit", "Satzstellung: Verb auf Position 2",
        "در جمله‌ی خبری آلمانی، فعل صرف‌شده همیشه در جایگاه دوم است. اگر جمله با قید زمان یا مکان شروع شود، فاعل "
        "بعد از فعل می‌آید: «Am Montag beginnt der Streik.» (نه «Am Montag der Streik beginnt»).",
        "Am Montag beginnt der Streik.",
        _p(r"^(Am|Im|In|Seit|Nach|Vor|Heute|Gestern|Morgen|Jetzt|Dort|Hier|Bei|Wegen|Trotz|Ab|Bis|Zum|Zur|Am\s+\w+)\b",
           flags=0),
    ),
]

TOPICS: Dict[str, List[GrammarTopic]] = {"de": GERMAN_TOPICS}


def topics_for(language: str) -> List[GrammarTopic]:
    return TOPICS.get(language, [])


def find_topic(language: str, topic_id: str) -> Optional[GrammarTopic]:
    return next((t for t in topics_for(language) if t.id == topic_id), None)
