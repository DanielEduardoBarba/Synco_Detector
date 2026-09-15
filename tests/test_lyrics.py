from synco_detector.transcription.lyrics import junk_lyrics


def test_junk_rejects_looped_hallucinations():
    assert junk_lyrics("And I've been to the end of the end of the end of the end of the end")
    assert junk_lyrics("Yes, yes, yes, yes, yes, yes, yes, yes, yes, yes")
    assert junk_lyrics("the the the the the the the the")
    assert junk_lyrics("Thanks for watching!")
    assert junk_lyrics("Happy birthday! Happy birthday!")
    assert junk_lyrics("Please subscribe!")


def test_junk_keeps_real_lines():
    assert not junk_lyrics("Jesus loves me this I know")
    assert not junk_lyrics("for the Bible tells me so")
    assert not junk_lyrics("Yes Jesus loves me")
