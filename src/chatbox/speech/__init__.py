from chatbox.speech.base import SpeechError, SpeechToText, TextToSpeech, Transcript

__all__ = ["SpeechError", "SpeechToText", "TextToSpeech", "Transcript", "make_stt", "make_tts"]


def make_stt(name: str, settings: dict) -> SpeechToText:
    if name == "google":
        from chatbox.speech.google import GoogleSpeechToText

        return GoogleSpeechToText(**settings)
    raise ValueError(f"unknown speech-to-text {name!r} (available: google)")


def make_tts(name: str, settings: dict) -> TextToSpeech:
    if name == "google":
        from chatbox.speech.google import GoogleTextToSpeech

        return GoogleTextToSpeech(**settings)
    raise ValueError(f"unknown text-to-speech {name!r} (available: google)")
