"""Bangla / Banglish text processing tests."""

from __future__ import annotations

import pytest

from utils.text import (
    classify_topic,
    detect_language,
    extract_crypto_terms,
    extract_keywords,
    is_question,
    jaccard,
    normalize_text,
    sanitize_incoming_text,
    text_similarity,
    trigram_similarity,
)


class TestQuestionDetection:
    @pytest.mark.parametrize("text", [
        "BTC এখন entry নেওয়া যাবে?",
        "ETH এখন কেমন?",
        "vai BTC neoa jabe ki",
        "Should I buy BTC now?",
        "মার্কেট কি উপরে যাবে",
        "bhai ki korbo ekhon",
        "TP koto hobe?",
        "অনুগ্রহ করে বলবেন আজকের মার্কেট কেমন হবে",
        "any idea about ETH?",
        "BTC 100k e jabe naki?",
    ])
    def test_questions_detected(self, text):
        assert is_question(text) is True

    @pytest.mark.parametrize("text", [
        "আজকের সাইকেল চালিয়ে আসলাম",
        "Good morning everyone",
        "ধন্যবাদ ভাই",
        "গতকাল রাতে বৃষ্টি হয়েছে",
        "BTC 67000 এ ছিল",
        "👍👍",
        "",
        None,
    ])
    def test_non_questions(self, text):
        assert is_question(text) is False


class TestLanguage:
    def test_bengali(self):
        assert detect_language("বিটকয়েন এখন কেমন আছে?") == "bn"

    def test_banglish(self):
        assert detect_language("btc ekhon kemon ache bhai entry nibo ki") == "banglish"

    def test_english(self):
        assert detect_language("Is bitcoin going to break resistance today") == "en"

    def test_mixed(self):
        assert detect_language("BTC এখন entry নেওয়া যাবে কি? (cross check)") in {"mixed", "bn"}


class TestKeywordsAndTerms:
    def test_keywords_remove_stopwords(self):
        keywords = extract_keywords("vai BTC এখন entry নেওয়া যাবে কি না?")
        assert "btc" in keywords and "entry" in keywords
        assert "vai" not in keywords

    def test_crypto_terms_and_aliases(self):
        terms = extract_crypto_terms("Bitcoin এর support আর Ethereum এর resistance কেমন?")
        assert "btc" in terms and "eth" in terms
        assert "support" in terms and "resistance" in terms

    def test_alias_stop_loss(self):
        assert "sl" in extract_crypto_terms("stop loss kothay dibo")

    def test_uppercase_ticker(self):
        assert "pepe" in extract_crypto_terms("PEPE uthbe ki?")


class TestTopics:
    @pytest.mark.parametrize("text,expected", [
        ("BTC entry নেওয়া যাবে? TP SL কত?", "entry_exit"),
        ("support resistance break hoise", "market_structure"),
        ("futures e leverage komau, funding rate negative", "futures_leverage"),
        ("নতুন airdrop এর খবর আছে?", "altcoin"),
        ("risk management chara trading korle loss hobe", "risk_management"),
        ("আসসালামু আলাইকুম ভাই", "greeting"),
    ])
    def test_classification(self, text, expected):
        assert classify_topic(text) == expected


class TestSimilarity:
    def test_jaccard(self):
        assert jaccard("btc entry now", "btc entry now") == 1.0
        assert jaccard("btc entry", "eth long") == 0.0
        assert 0 < jaccard("btc entry now", "btc entry later") < 1

    def test_trigram_handles_typos(self):
        assert trigram_similarity("bitcoin kemon ache", "bitcoin kemon ase") > 0.7

    def test_text_similarity(self):
        assert text_similarity("BTC এখন entry নেওয়া যাবে?", "btc ekhon entry neowa jabe?") < 0.5
        assert text_similarity("BTC entry এখন", "BTC entry এখন") == 1.0


class TestSanitisation:
    def test_control_chars_and_zero_width_removed(self):
        clean = sanitize_incoming_text("hi\x00\x07 there\u200b friend")
        assert "\x00" not in clean and "\u200b" not in clean

    def test_length_limit(self):
        assert len(sanitize_incoming_text("a" * 100, max_chars=10)) == 10

    def test_bengali_digits_normalised(self):
        assert "1234" in normalize_text("সংখ্যা ১২৩৪")

    def test_normalize_collapses_case_and_punctuation(self):
        assert normalize_text("BTC, Entry!! NOW?") == "btc entry now"
