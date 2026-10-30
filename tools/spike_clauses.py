# -*- coding: utf-8 -*-
"""30 representative counsellor clauses (hand-written test text, Devanagari with English course terms, as the Devanagari flow produces them) in four
length classes. Used only by tools/phase0_streaming_spike.py; not caller data."""

CLAUSES = {
    4: ["जी, ज़रूर बताइए ना।", "आपका नाम क्या है?", "हॉस्टल की सुविधा है।", "आख़िरी तारीख़ तीस जून।",
        "कौन सा course चाहिए?", "बारहवीं में कितने प्रतिशत?", "जी, मैं समझ गया।", "धन्यवाद, शुभ दिन रहे।"],
    8: ["आप बारहवीं के बाद क्या करना चाहते हैं?", "सालाना फ़ीस लगभग एक लाख बीस हज़ार है।", "हॉस्टल में रहने और खाने की सुविधा है।",
        "admission के लिए बारहवीं की marksheet चाहिए होगी।", "merit वाले students को scholarship मिल सकती है।", "मैं आपको पूरी जानकारी अभी बता देता हूँ।",
        "आप अपना phone number बता सकते हैं क्या?", "counselling सोमवार से शुक्रवार तक online होती है।"],
    12: ["B.Tech चार साल का course है, admission entrance exam से होता है।", "अगर आपके साठ प्रतिशत से ज़्यादा नंबर हैं apply कर सकते हैं।",
         "हमारे campus में library, lab और sports की पूरी सुविधा मौजूद है।", "registration की आख़िरी तारीख़ तीस जून है, उससे पहले form भर दीजिए।",
         "placement में पिछले साल बयासी प्रतिशत students को अच्छी नौकरी मिली थी।", "आप चाहें तो मैं आपको पूरी fee structure अभी समझा सकता हूँ।",
         "आपको ये ज़रूरी documents लाने होंगे: marksheet, ID proof और दो photographs।"],
    16: ["अगर आपके बारहवीं में साठ प्रतिशत से ज़्यादा नंबर हैं तो आप apply कर सकते हैं।",
         "सालाना फ़ीस लगभग एक लाख बीस हज़ार रुपये है, hostel की fee अलग से लगती है।",
         "हमारे यहाँ computer science, mechanical और civil जैसी कई branches में B.Tech की पढ़ाई होती है।",
         "आप online form भर दीजिए, फिर हमारी team आपको counselling के लिए जल्दी ही call करेगी।",
         "scholarship उन students को मिलती है जिनके बारहवीं में नब्बे प्रतिशत या उससे ज़्यादा नंबर हों।",
         "campus में hostel, mess, Wi-Fi और चौबीस घंटे security है, इसलिए आपको कोई परेशानी नहीं होगी।",
         "मैं आपको admission process step by step समझा देता हूँ, पहले आप अपना form भर लीजिए।"],
}

if __name__ == "__main__":
    for n, items in CLAUSES.items():
        print(n, len(items), [len(t.split()) for t in items])
