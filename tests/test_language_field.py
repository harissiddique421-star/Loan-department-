"""Language dropdown on the loan form (Urdu / English / Sindhi / Other + typed language)."""
import json

from conftest import new_app


def form_of(appm, pk):
    with appm.app.app_context():
        return json.loads(appm.db.session.get(appm.Application, pk).form_json or "{}")


def test_form_shows_language_dropdown(staff):
    pk = new_app(staff, complete=False)
    page = staff.get(f"/application/{pk}/form").get_data(as_text=True)
    assert 'name="language"' in page and 'name="languageOther"' in page
    for opt in ("Urdu", "English", "Sindhi", "Other"):
        assert f'<option value="{opt}"' in page


def test_other_language_saved_and_shown(appm, admin, staff):
    pk = new_app(staff, complete=False, language="Other", languageOther="Punjabi")
    vals = form_of(appm, pk)
    assert vals["language"] == "Other" and vals["languageOther"] == "Punjabi"
    page = admin.get(f"/application/{pk}").get_data(as_text=True)
    assert "Language (زبان)" in page and "Other — Punjabi" in page


def test_typed_language_dropped_unless_other(appm, staff):
    pk = new_app(staff, complete=False, language="Sindhi", languageOther="leftover")
    vals = form_of(appm, pk)
    assert vals["language"] == "Sindhi" and vals["languageOther"] == ""


def test_unknown_language_choice_rejected(appm, staff):
    pk = new_app(staff, complete=False, language="Klingon")
    assert form_of(appm, pk)["language"] == ""


def test_language_printed_on_word_form():
    import io, zipfile
    import word_form
    assert word_form.printed_value({"language": "Other", "languageOther": "Punjabi"}, "language") == "Punjabi"
    assert word_form.printed_value({"language": "Sindhi", "languageOther": ""}, "language") == "Sindhi"
    data = word_form.build({"app_id": "TF-TEST", "values": {"name": "A", "language": "Other",
                                                             "languageOther": "Punjabi"}})
    xml = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode()
    assert "زبان :" in xml and ">Punjabi<" in xml
    assert word_form.verify(data, "TF-TEST", {"language": "Other", "languageOther": "Punjabi"}) == []
