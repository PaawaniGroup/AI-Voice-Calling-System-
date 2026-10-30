# -*- coding: utf-8 -*-
"""
mme_hinglish.py - two small text helpers for Hindi/Hinglish TTS, used AFTER mme_frontend.normalize():

  roman_to_mixed(text)    Roman-script Hindi words -> Devanagari ("Aapka admission kaise hoga" -> "आपका admission कैसे होगा"). English words stay Latin.
                          Only words found in HINDI_ROMAN are converted: Hinglish function words, verbs and everyday nouns are a closed set, English words are not in it.
  respell_english(text)   common counselling English words -> the Devanagari spelling Indian speakers use ("question" -> "क्वेश्चन"). Optional: test it, it may or may not help.

Both are lexicon look-ups (no model). The tables were written from memory: a native Hindi speaker should review them, and the unknown-word report
(unknown_roman_words) shows which Latin words were left alone so you can add the missing Hindi ones.
"""
import re

HINDI_ROMAN = {
 # pronouns / determiners
 "main": "मैं", "mai": "मैं", "mujhe": "मुझे", "mera": "मेरा", "meri": "मेरी", "mere": "मेरे", "hum": "हम", "humein": "हमें", "hame": "हमें", "humne": "हमने", "humse": "हमसे",
 "hamara": "हमारा", "hamari": "हमारी", "hamare": "हमारे", "tum": "तुम", "tumhara": "तुम्हारा", "tumhari": "तुम्हारी", "tumhare": "तुम्हारे", "tumhe": "तुम्हें",
 "aap": "आप", "aapka": "आपका", "aapki": "आपकी", "aapke": "आपके", "aapko": "आपको", "aapse": "आपसे", "aapne": "आपने", "apna": "अपना", "apni": "अपनी", "apne": "अपने",
 "woh": "वो", "wo": "वो", "yeh": "यह", "ye": "ये", "yahan": "यहाँ", "wahan": "वहाँ", "wahin": "वहीं", "yahin": "यहीं", "idhar": "इधर", "udhar": "उधर",
 "unka": "उनका", "unki": "उनकी", "unke": "उनके", "unse": "उनसे", "unhein": "उन्हें", "iska": "इसका", "iski": "इसकी", "iske": "इसके", "uska": "उसका", "uski": "उसकी", "uske": "उसके",
 "ise": "इसे", "isse": "इससे", "usse": "उससे", "us": "उस", "is": None, "kisi": "किसी", "kis": "किस", "kisko": "किसको", "kiska": "किसका", "jo": "जो", "jis": "जिस", "jise": "जिसे", "jisme": "जिसमें",
 "koi": "कोई", "kuch": "कुछ", "sab": "सब", "sabhi": "सभी", "saari": "सारी", "saara": "सारा", "har": "हर", "dono": "दोनों", "doosre": "दूसरे", "doosra": "दूसरा", "doosron": "दूसरों", "kai": "कई",
 "kya": "क्या", "kyaa": "क्या", "kaise": "कैसे", "kaisa": "कैसा", "kaisi": "कैसी", "kab": "कब", "kahan": "कहाँ", "kaun": "कौन", "kyun": "क्यों", "kyon": "क्यों", "kyunki": "क्योंकि", "kitna": "कितना", "kitni": "कितनी", "kitne": "कितने",
 "aisa": "ऐसा", "aisi": "ऐसी", "aise": "ऐसे", "jaisa": "जैसा", "jaisi": "जैसी", "jaise": "जैसे", "waise": "वैसे", "vaise": "वैसे", "itna": "इतना", "itni": "इतनी", "itne": "इतने", "utna": "उतना", "utne": "उतने",
 # postpositions / conjunctions / particles
 "ka": "का", "ki": "की", "ke": "के", "ko": "को", "se": "से", "mein": "में", "men": "में", "par": "पर", "tak": "तक", "liye": "लिए", "saath": "साथ", "baare": "बारे", "baad": "बाद", "pehle": "पहले",
 "andar": "अंदर", "bahar": "बाहर", "beech": "बीच", "taraf": "तरफ", "bina": "बिना", "sirf": "सिर्फ", "bhi": "भी", "hi": "ही", "na": "ना", "nahi": "नहीं", "nahin": "नहीं", "mat": "मत", "ya": "या", "aur": "और",
 "lekin": "लेकिन", "magar": "मगर", "kyunki ": "क्योंकि", "isliye": "इसलिए", "isliya": "इसलिए", "taaki": "ताकि", "agar": "अगर", "toh": "तो", "jab": "जब", "tab": "तब", "warna": "वरना", "balki": "बल्कि", "phir": "फिर", "fir": "फिर",
 "ab": "अब", "abhi": "अभी", "kabhi": "कभी", "hamesha": "हमेशा", "bilkul": "बिल्कुल", "bahut": "बहुत", "bohot": "बहुत", "thoda": "थोड़ा", "thodi": "थोड़ी", "thode": "थोड़े", "zyada": "ज़्यादा", "jyada": "ज़्यादा", "kam": "कम",
 "zaroor": "ज़रूर", "jaroor": "ज़रूर", "zaroori": "ज़रूरी", "zaroorat": "ज़रूरत", "sahi": "सही", "galat": "गलत", "sabse": "सबसे", "sath": "साथ", "wala": "वाला", "wali": "वाली", "wale": "वाले", "bas": "बस", "waqt": "वक़्त", "samay": "समय",
 "aaj": "आज", "kal": "कल", "aajkal": "आजकल", "parso": "परसों", "subah": "सुबह", "shaam": "शाम", "raat": "रात", "dopahar": "दोपहर", "roz": "रोज़", "agla": "अगला", "agli": "अगली", "aakhri": "आखिरी", "pehla": "पहला", "pehli": "पहली",
 # be / do / have verbs
 "hai": "है", "hain": "हैं", "hoon": "हूँ", "ho": "हो", "tha": "था", "thi": "थी", "the": None, "hoga": "होगा", "hogi": "होगी", "honge": "होंगे", "hongi": "होंगी", "hona": "होना", "hota": "होता", "hoti": "होती", "hote": "होते", "hua": "हुआ", "hui": "हुई", "hue": "हुए",
 "karna": "करना", "karne": "करने", "karni": "करनी", "karta": "करता", "karti": "करती", "karte": "करते", "kar": "कर", "karo": "करो", "kariye": "कीजिए", "kijiye": "कीजिए", "karenge": "करेंगे", "karunga": "करूँगा", "karungi": "करूँगी", "karke": "करके", "kiya": "किया", "kiye": "किए",
 "jana": "जाना", "jaana": "जाना", "jaata": "जाता", "jaati": "जाती", "jaate": "जाते", "jaaye": "जाए", "jaye": "जाए", "jaayega": "जाएगा", "jayega": "जाएगा", "jaayegi": "जाएगी", "jayegi": "जाएगी", "jaiye": "जाइए", "gaya": "गया", "gayi": "गई", "gaye": "गए", "jaane": "जाने", "jaan": "जान",
 "aana": "आना", "aane": "आने", "aate": "आते", "aaye": "आए", "aayega": "आएगा", "aayegi": "आएगी", "aaiye": "आइए", "aata": "आता", "aati": "आती",
 "lena": "लेना", "lene": "लेने", "lete": "लेते", "leta": "लेता", "leti": "लेती", "lijiye": "लीजिए", "liya": "लिया", "le": "ले", "lag": "लग", "lagta": "लगता", "lagti": "लगती", "lagega": "लगेगा", "lagatar": "लगातार", "laga": "लगा",
 "dena": "देना", "dene": "देने", "dete": "देते", "deta": "देता", "deti": "देती", "dijiye": "दीजिए", "diya": "दिया", "diye": "दिए", "denge": "देंगे", "di": "दी", "dikhein": "दिखें", "dikhna": "दिखना",
 "dekhna": "देखना", "dekhiye": "देखिए", "dekhte": "देखते", "dekh": "देख", "dekhein": "देखें", "bata": "बता", "bataiye": "बताइए", "bataye": "बताए", "batana": "बताना", "bataen": "बताएँ", "suniye": "सुनिए", "sunkar": "सुनकर", "sunna": "सुनना",
 "padhna": "पढ़ना", "padhne": "पढ़ने", "padhte": "पढ़ते", "padh": "पढ़", "padhai": "पढ़ाई", "likhna": "लिखना", "likhiye": "लिखिए", "likh": "लिख", "sochna": "सोचना", "sochiye": "सोचिए", "soch": "सोच",
 "samajh": "समझ", "samajhna": "समझना", "samajhte": "समझते", "samjha": "समझा", "samjhaiye": "समझाइए", "samajhdaari": "समझदारी", "milna": "मिलना", "mil": "मिल", "mile": "मिले", "milkar": "मिलकर", "milega": "मिलेगा",
 "rakhna": "रखना", "rakhiye": "रखिए", "rakh": "रख", "rakho": "रखो", "bolna": "बोलना", "boliye": "बोलिए", "bol": "बोल", "chahiye": "चाहिए", "chahte": "चाहते", "chahta": "चाहता", "chahti": "चाहती",
 "sakta": "सकता", "sakti": "सकती", "sakte": "सकते", "sakenge": "सकेंगे", "rehna": "रहना", "rehta": "रहता", "rehti": "रहती", "rahe": "रहे", "rahi": "रही", "raha": "रहा", "rahiye": "रहिए", "chalna": "चलना", "chaliye": "चलिए", "chalte": "चलते",
 "banana": "बनाना", "banaiye": "बनाइए", "banaya": "बनाया", "banaye": "बनाए", "bana": "बना", "bane": "बने", "banti": "बनती", "banta": "बनता", "badalna": "बदलना", "badal": "बदल", "badhenge": "बढ़ेंगे", "badhna": "बढ़ना",
 "bharna": "भरना", "bharte": "भरते", "bharo": "भरो", "bhejna": "भेजना", "bhej": "भेज", "bhejiye": "भेजिए", "chunna": "चुनना", "chunne": "चुनने", "chunte": "चुनते", "chhodiye": "छोड़िए", "chhodna": "छोड़ना", "pahuncha": "पहुँचा", "paane": "पाने", "pana": "पाना",
 "maangna": "माँगना", "jeet": "जीत", "daalne": "डालने", "darne": "डरने", "baithiye": "बैठिए", "baithenge": "बैठेंगे", "pehchaniye": "पहचानिए", "seekhiye": "सीखिए", "seekhne": "सीखने", "sehne": "सहने", "roke": "रोके", "ginkar": "गिनकर", "kharch": "खर्च",
 "bachche": "बच्चे", "bachcha": "बच्चा", "dost": "दोस्त", "doston": "दोस्तों", "ghar": "घर", "kaam": "काम", "naam": "नाम", "jagah": "जगह", "baat": "बात", "baatein": "बातें", "baatcheet": "बातचीत", "din": "दिन", "dinon": "दिनों", "saal": "साल", "hafte": "हफ़्ते", "hafta": "हफ़्ता", "ghante": "घंटे", "ghanta": "घंटा",
 "paisa": "पैसा", "paise": "पैसे", "rupaye": "रुपये", "rupaya": "रुपया", "hazaar": "हज़ार", "lakh": "लाख", "crore": "करोड़", "pratishat": "प्रतिशत", "kul": "कुल",
 "neend": "नींद", "bhookh": "भूख", "chinta": "चिंता", "ghabrahat": "घबराहट", "pareshan": "परेशान", "dikkat": "दिक्कत", "madad": "मदद", "koshish": "कोशिश", "mehnat": "मेहनत", "dhyan": "ध्यान", "bharosa": "भरोसा", "aaram": "आराम", "asar": "असर", "hissa": "हिस्सा", "ruchi": "रुचि",
 "raaste": "रास्ते", "raasta": "रास्ता", "kadam": "कदम", "tarikh": "तारीख", "hisaab": "हिसाब", "akelapan": "अकेलापन", "akele": "अकेले", "alag": "अलग", "achha": "अच्छा", "achhe": "अच्छे", "achhi": "अच्छी", "acha": "अच्छा", "bura": "बुरा", "bada": "बड़ा", "chhota": "छोटा", "chhote": "छोटे", "chhoti": "छोटी",
 "naya": "नया", "nayi": "नई", "purane": "पुराने", "purana": "पुराना", "behtar": "बेहतर", "saaf": "साफ़", "poori": "पूरी", "poora": "पूरा", "maujood": "मौजूद", "nirbhar": "निर्भर", "jayaz": "जायज़", "dheere": "धीरे", "chupchap": "चुपचाप", "waise ": "वैसे",
 "namaste": "नमस्ते", "kripya": "कृपया", "dhanyavaad": "धन्यवाद", "shukriya": "शुक्रिया", "maaf": "माफ़", "swagat": "स्वागत", "baatchit": "बातचीत", "awaaz": "आवाज़", "aadhar": "आधार", "naukri": "नौकरी",
 # numbers
 "ek": "एक", "teen": "तीन", "chaar": "चार", "paanch": "पाँच", "chhe": "छह", "chhah": "छह", "saat": "सात", "aath": "आठ", "nau": "नौ", "das": "दस", "gyarah": "ग्यारह", "barah": "बारह", "barahvin": "बारहवीं", "pandrah": "पंद्रह", "bees": "बीस", "paintalis": "पैंतालीस", "pachaas": "पचास", "tees": "तीस", "sau": "सौ",
 "baar": "बार", "bajaye": "बजाय", "baje": "बजे", "banati": "बनाती", "bitaya": "बिताया", "dobara": "दोबारा", "galtiyan": "गलतियाँ", "jama": "जमा", "mehsoos": "महसूस", "paas": "पास", "pasand": "पसंद",
 "sa": "सा", "saans": "साँस", "sambhal": "संभाल", "shuru": "शुरू", "tarah": "तरह", "teeno": "तीनों", "ginna": "गिनना", "bitana": "बिताना", "samay ": "समय", "jaldi": "जल्दी", "theek": "ठीक", "thik": "ठीक",
 "kahani": "कहानी", "sapna": "सपना", "sapne": "सपने", "log": "लोग", "logon": "लोगों", "cheez": "चीज़", "cheezein": "चीज़ें", "mushkil": "मुश्किल", "aasaan": "आसान", "asaan": "आसान", "zindagi": "ज़िंदगी", "dil": "दिल", "dimaag": "दिमाग",
 "pichhla": "पिछला", "aage": "आगे", "kuchh": "कुछ", "yaad": "याद", "jhijhak": "झिझक", "taiyaar": "तैयार", "tikna": "टिकना", "waha": "वहाँ", "uske": "उसके", "inka": "इनका", "inki": "इनकी", "inke": "इनके", "isi": "इसी", "usi": "उसी",
}
HINDI_ROMAN = {k.strip(): v for k, v in HINDI_ROMAN.items() if v}

ENGLISH_DEV = {
 "admission": "एडमिशन", "application": "एप्लीकेशन", "aptitude": "एप्टीट्यूड", "career": "करियर", "certificate": "सर्टिफ़िकेट", "charges": "चार्जेस", "commerce": "कॉमर्स", "communication": "कम्युनिकेशन",
 "company": "कंपनी", "concern": "कंसर्न", "confidence": "कॉन्फिडेंस", "confirm": "कन्फ़र्म", "contact": "कॉन्टैक्ट", "counselling": "काउंसलिंग", "counseling": "काउंसलिंग", "counsellor": "काउंसलर", "course": "कोर्स", "courses": "कोर्सेस",
 "decision": "डिसीज़न", "decisions": "डिसीज़न्स", "degree": "डिग्री", "details": "डिटेल्स", "distraction": "डिस्ट्रैक्शन", "documents": "डॉक्यूमेंट्स", "document": "डॉक्यूमेंट", "education": "एजुकेशन", "email": "ईमेल",
 "entrance": "एंट्रेंस", "exam": "एग्ज़ाम", "exams": "एग्ज़ाम्स", "expenses": "एक्सपेंसेस", "experience": "एक्सपीरियंस", "family": "फ़ैमिली", "fee": "फ़ीस", "fees": "फ़ीस", "form": "फ़ॉर्म", "hostel": "हॉस्टल",
 "interest": "इंटरेस्ट", "internship": "इंटर्नशिप", "interview": "इंटरव्यू", "loan": "लोन", "management": "मैनेजमेंट", "marks": "मार्क्स", "marksheet": "मार्कशीट", "maths": "मैथ्स", "merit": "मेरिट", "mobile": "मोबाइल",
 "notes": "नोट्स", "number": "नंबर", "options": "ऑप्शंस", "option": "ऑप्शन", "passport": "पासपोर्ट", "placement": "प्लेसमेंट", "plan": "प्लान", "practice": "प्रैक्टिस", "pressure": "प्रेशर", "problem": "प्रॉब्लम", "problems": "प्रॉब्लम्स",
 "question": "क्वेश्चन", "questions": "क्वेश्चन्स", "registration": "रजिस्ट्रेशन", "result": "रिज़ल्ट", "resume": "रिज़्यूमे", "scholarship": "स्कॉलरशिप", "science": "साइंस", "session": "सेशन", "software": "सॉफ़्टवेयर", "stress": "स्ट्रेस",
 "students": "स्टूडेंट्स", "student": "स्टूडेंट", "subject": "सब्जेक्ट", "thanks": "थैंक्स", "time": "टाइम", "university": "यूनिवर्सिटी", "universities": "यूनिवर्सिटीज़", "visa": "वीज़ा", "please": "प्लीज़", "sorry": "सॉरी", "ok": "ओके",
}

# Extension from the word-frequency list of the 272 Hindi/Hinglish counselling sentences (words still in Latin after level 3). Domain nouns/verbs/adjectives only:
# English function words (but, so, and, to, because, of, you, ...) are deliberately NOT respelled. Written from memory: review with a native speaker.
ENGLISH_DEV_EXTRA = {
 "skills": "स्किल्स", "share": "शेयर", "test": "टेस्ट", "check": "चेक", "study": "स्टडी", "break": "ब्रेक", "strengths": "स्ट्रेंथ्स", "real": "रियल", "date": "डेट", "total": "टोटल", "review": "रिव्यू", "minute": "मिनट", "minutes": "मिनट",
 "feel": "फ़ील", "step": "स्टेप", "steps": "स्टेप्स", "ability": "एबिलिटी", "patience": "पेशेंस", "registered": "रजिस्टर्ड", "topic": "टॉपिक", "class": "क्लास", "classes": "क्लासेस", "next": "नेक्स्ट", "matter": "मैटर", "coding": "कोडिंग",
 "last": "लास्ट", "year": "ईयर", "list": "लिस्ट", "phone": "फ़ोन", "try": "ट्राई", "natural": "नैचुरल", "parents": "पेरेंट्स", "future": "फ़्यूचर", "social": "सोशल", "media": "मीडिया", "help": "हेल्प", "cost": "कॉस्ट", "living": "लिविंग", "call": "कॉल",
 "hello": "हैलो", "confidential": "कॉन्फिडेंशियल", "stream": "स्ट्रीम", "streams": "स्ट्रीम्स", "welcome": "वेलकम", "salary": "सैलरी", "solving": "सॉल्विंग", "solve": "सॉल्व", "data": "डेटा", "shortlist": "शॉर्टलिस्ट", "backup": "बैकअप",
 "field": "फ़ील्ड", "fields": "फ़ील्ड्स", "arts": "आर्ट्स", "syllabus": "सिलेबस", "record": "रिकॉर्ड", "program": "प्रोग्राम", "compulsory": "कंपल्सरी", "march": "मार्च", "submit": "सबमिट", "proof": "प्रूफ़", "size": "साइज़", "photos": "फ़ोटोज़",
 "rounds": "राउंड्स", "birth": "बर्थ", "installments": "इंस्टॉलमेंट्स", "pay": "पे", "rate": "रेट", "repayment": "रीपेमेंट", "period": "पीरियड", "affordable": "अफ़ॉर्डेबल", "balanced": "बैलेंस्ड", "target": "टारगेट", "targets": "टारगेट्स",
 "previous": "प्रीवियस", "papers": "पेपर्स", "paper": "पेपर", "pattern": "पैटर्न", "week": "वीक", "end": "एंड", "revise": "रिवाइज़", "score": "स्कोर", "revision": "रिविज़न", "short": "शॉर्ट", "fixed": "फ़िक्स्ड", "fix": "फ़िक्स", "mind": "माइंड",
 "routine": "रूटीन", "set": "सेट", "room": "रूम", "sleep": "स्लीप", "memory": "मेमोरी", "concentration": "कॉन्सन्ट्रेशन", "deep": "डीप", "breathing": "ब्रीदिंग", "professional": "प्रोफ़ेशनल", "comparison": "कंपैरिजन", "define": "डिफ़ाइन",
 "small": "स्मॉल", "celebrate": "सेलिब्रेट", "waste": "वेस्ट", "preparation": "प्रिपरेशन", "teacher": "टीचर", "peer": "पीयर", "limit": "लिमिट", "trusted": "ट्रस्टेड", "member": "मेंबर", "best": "बेस्ट", "job": "जॉब", "technical": "टेक्निकल",
 "research": "रिसर्च", "page": "पेज", "projects": "प्रोजेक्ट्स", "work": "वर्क", "strong": "स्ट्रॉन्ग", "abroad": "अब्रॉड", "english": "इंग्लिश", "bank": "बैंक", "statement": "स्टेटमेंट", "letter": "लेटर", "country": "कंट्री", "calculate": "कैलकुलेट",
 "tuesday": "ट्यूज़डे", "available": "अवेलेबल", "schedule": "शेड्यूल", "miss": "मिस", "report": "रिपोर्ट", "team": "टीम", "hurry": "हरी", "line": "लाइन", "repeat": "रिपीट", "query": "क्वेरी", "senior": "सीनियर", "bright": "ब्राइट", "weakness": "वीकनेस",
 "percent": "परसेंट", "interests": "इंटरेस्ट्स", "goals": "गोल्स", "decide": "डिसाइड", "keep": "कीप", "ready": "रेडी", "subjects": "सब्जेक्ट्स", "choose": "चूज़", "personality": "पर्सनैलिटी", "path": "पाथ", "rank": "रैंक", "engineering": "इंजीनियरिंग",
 "design": "डिज़ाइन", "law": "लॉ", "explore": "एक्सप्लोर", "twelfth": "ट्वेल्थ", "difficulty": "डिफ़िकल्टी", "focus": "फ़ोकस", "accounts": "अकाउंट्स", "free": "फ़्री", "demo": "डेमो", "attend": "अटेंड", "final": "फ़ाइनल", "name": "नेम",
 "upload": "अपलोड", "confirmation": "कन्फ़र्मेशन", "message": "मैसेज", "seat": "सीट", "acceptance": "एक्सेप्टेंस", "apply": "अप्लाई", "income": "इनकम", "attach": "अटैच", "otherwise": "अदरवाइज़", "reject": "रिजेक्ट", "colleges": "कॉलेजेस", "mock": "मॉक",
 "weak": "वीक", "areas": "एरियाज़", "simple": "सिंपल", "timetable": "टाइमटेबल", "technique": "टेक्निक", "focused": "फ़ोकस्ड", "points": "पॉइंट्स", "chapter": "चैप्टर", "copy": "कॉपी", "anxiety": "एंग्ज़ायटी", "appetite": "एपेटाइट",
 "medical": "मेडिकल", "treatment": "ट्रीटमेंट", "expert": "एक्सपर्ट", "child": "चाइल्ड", "scope": "स्कोप", "detail": "डिटेल", "bad": "बैड", "mistakes": "मिस्टेक्स", "confident": "कॉन्फिडेंट", "habit": "हैबिट", "loneliness": "लोनलीनेस", "friend": "फ़्रेंड",
 "life": "लाइफ़", "compare": "कंपेयर", "hobbies": "हॉबीज़", "music": "म्यूज़िक", "sports": "स्पोर्ट्स", "drawing": "ड्रॉइंग", "group": "ग्रुप", "discussion": "डिस्कशन", "average": "एवरेज", "package": "पैकेज", "role": "रोल", "depend": "डिपेंड",
 "indian": "इंडियन", "germany": "जर्मनी", "public": "पब्लिक", "tuition": "ट्यूशन", "zero": "ज़ीरो", "back": "बैक", "whatsapp": "व्हाट्सऐप", "preferred": "प्रेफ़र्ड", "slot": "स्लॉट", "honestly": "ऑनेस्टली", "choice": "चॉइस", "voice": "वॉइस", "cut": "कट",
 "network": "नेटवर्क", "connect": "कनेक्ट", "request": "रिक्वेस्ट", "note": "नोट", "ticket": "टिकट", "use": "यूज़", "follow": "फ़ॉलो", "homework": "होमवर्क", "helpline": "हेल्पलाइन", "open": "ओपन", "batch": "बैच", "start": "स्टार्ट", "card": "कार्ड", "collect": "कलेक्ट",
}

PHRASES = {"thank you": "थैंक यू"}


def _sub_words(text, table, skip_capital_words=()):
    def rep(m):
        w = m.group(0)
        if w in skip_capital_words:
            return w
        return table.get(w.lower(), w)
    return re.sub(r"[A-Za-z]+", rep, text)


DO_NEXT = {"minute", "minutes", "hours", "hour", "days", "weeks", "months", "years", "options", "log", "logon"}


def roman_to_mixed(text: str) -> str:
    """Roman-script Hindi words -> Devanagari; every other Latin word is left as it is."""
    out = _sub_words(text, HINDI_ROMAN, skip_capital_words=("Hi", "Is", "The", "Us"))
    # "do" is both Hindi (दो = two) and English: convert it only before a Hindi word (already Devanagari after the pass above) or a counted noun
    out = re.sub(r"\b[Dd]o(?=\s+(?:[\u0900-\u097F]|(?:" + "|".join(DO_NEXT) + r")\b))", "दो", out)
    return out


def respell_english(text: str, extra: bool = False) -> str:
    table = {**ENGLISH_DEV, **ENGLISH_DEV_EXTRA} if extra else ENGLISH_DEV
    t = text
    for k, v in PHRASES.items():
        t = re.sub(r"\b" + k + r"\b", v, t, flags=re.I)
    return _sub_words(t, table)


def unknown_roman_words(text: str):
    """Latin words that neither table knows (English words, or Hindi words missing from HINDI_ROMAN)."""
    return [w.lower() for w in re.findall(r"[A-Za-z]+", text) if w.lower() not in HINDI_ROMAN and w.lower() not in ENGLISH_DEV]


def prepare(text: str, level: int = 2) -> str:
    """level 1 = numbers/money/dates/phones/acronyms only; 2 = + Roman Hindi words -> Devanagari; 3 = + 79 common English words respelled in Devanagari; 4 = level 3 + the extended table (about 250 more domain words).
    Plain-English sentences are never changed by levels 2 and 3."""
    import mme_frontend as F
    t = F.normalize(text)
    if F.detect_lang(text) == "en":
        return t
    if level >= 2:
        t = roman_to_mixed(t)
    if level >= 3:
        t = respell_english(t, extra=(level >= 4))
    return t


# ---------------------------------------------------------------------------------------------------------------------------------------------------
# Spelling variants and two context rules added after a listening report (aapko, hoon, han, sakta, chaiya, baare main, hota, hai, deta, ...):
# the model reads Roman letters with English sounds, so every common Hindi word must reach it in Devanagari, including the spellings an LLM or a speech model writes.
_VARIANTS = {
    "han": "हाँ", "haan": "हाँ", "haa": "हाँ", "haanji": "हाँ जी", "hanji": "हाँ जी",
    "chaiya": "चाहिए", "chahie": "चाहिए", "chaiye": "चाहिए", "chahiyee": "चाहिए", "chahiye": "चाहिए",
    "bare": "बारे", "baare": "बारे",
    "batata": "बताता", "batati": "बताती", "batate": "बताते", "bataunga": "बताऊँगा", "bataungi": "बताऊँगी", "batao": "बताओ",
    "bataiye": "बताइए", "batayiye": "बताइए", "bataye": "बताइए", "batayein": "बताएँ", "batayen": "बताएँ",
    "hun": "हूँ", "sakte": "सकते", "karte": "करते", "karti": "करती", "dete": "देते", "chahta": "चाहता", "chahti": "चाहती", "chahte": "चाहते",
    "lagta": "लगता", "lagti": "लगती", "milta": "मिलता", "milti": "मिलती", "jata": "जाता", "jati": "जाती", "jate": "जाते", "aata": "आता", "aati": "आती", "aate": "आते",
    "hoga": "होगा", "hogi": "होगी", "honge": "होंगे", "raha": "रहा", "rahi": "रही", "rahe": "रहे", "kijiye": "कीजिए", "kariye": "कीजिए", "samajh": "समझ", "samjhe": "समझे",
}
for _k, _v in _VARIANTS.items():
    HINDI_ROMAN.setdefault(_k, _v)

_POSTPOSITION_BEFORE = ("baare", "bare", "andar", "dauran", "beech", "baad", "pehle")          # "baare main" is a typo for "baare mein"
_ENGLISH_AFTER_MAIN = ("course", "campus", "branch", "building", "office", "gate", "page", "menu", "street", "road", "entrance", "hall")   # "main campus" is English
_roman_to_mixed_base = roman_to_mixed


def roman_to_mixed(text: str) -> str:
    """Roman Hindi words -> Devanagari.
    Protects proper education terms (JEE Main, JEE Mains, B.Tech, IELTS, TOEFL) so they
    are never incorrectly transliterated into Hindi pronouns (e.g. 'JEE मैं').
    Context rules for 'main': after baare/andar/... it is the typo for में; before course/campus/... it stays English.
    """
    saved_terms: dict[str, str] = {}

    def _protect(m: re.Match) -> str:
        tok = f"___PROT_{len(saved_terms)}___"
        saved_terms[tok] = m.group(0)
        return tok

    # Protect JEE Main / JEE Mains (case-insensitive)
    t = re.sub(r"\bJEE\s+Mains?\b", _protect, text, flags=re.I)
    # Protect B.Tech, IELTS, TOEFL
    t = re.sub(r"\b(?:B\.?Tech|IELTS|TOEFL)\b", _protect, t, flags=re.I)
    # Context rules for standalone "main"
    t = re.sub(r"\b(" + "|".join(_POSTPOSITION_BEFORE) + r")(\s+)main\b", r"\1\2mein", t, flags=re.I)
    t = re.sub(r"\bmain(?=\s+(?:" + "|".join(_ENGLISH_AFTER_MAIN) + r")\b)", "MAINENGLISH", t, flags=re.I)

    out = _roman_to_mixed_base(t).replace("MAINENGLISH", "main")
    for placeholder, original in saved_terms.items():
        out = out.replace(placeholder, original)
    return out
