from codex_uc.contracts import MeaningCheck, Rewrite
from codex_uc.writing import alerts, present


class Rewriter:
    def __init__(self, text, *, equivalent=True):
        self.text = text
        self.calls = 0
        self.equivalent = equivalent

    def ask(self, directory, prompt, contract, **kwargs):
        if contract == MeaningCheck:
            return MeaningCheck(
                same_meaning=self.equivalent,
                clearer_or_equal=True,
                reason="Compared both passages.",
            )
        self.calls += 1
        return Rewrite(paragraphs=[self.text], facts_preserved=True)


def test_writing_check_ignores_code_commands_and_quoted_evidence(tmp_path):
    text = "Use `utilize()` to inspect the result.\n\n```python\nleverage = 'it is worth noting'\n```\n\n> The old report said leverage."
    runtime = Rewriter(text)
    assert alerts(text) == []
    assert present(runtime, [text], tmp_path) == text
    assert runtime.calls == 1


def test_one_wording_rewrite_keeps_numbers_and_uncertainty(tmp_path):
    runtime = Rewriter("Use the result. It might reduce 2 requests.")
    text = present(runtime, ["Utilize the result. It might reduce 2 requests."], tmp_path)
    assert text == "Use the result. It might reduce 2 requests."
    assert runtime.calls == 1


def test_rewrite_cannot_invent_a_test_result_or_remove_uncertainty(tmp_path):
    original = "Utilize `test_value`. It might pass 2 checks. The result is unverified."
    for rewrite in (
        "Use `test_value`. It might pass 3 checks. The result is unverified.",
        "Use `test_value`. It will pass 2 checks. The result is verified.",
        "Use `different_test`. It might pass 2 checks. The result is unverified.",
    ):
        runtime = Rewriter(rewrite)
        assert present(runtime, [original], tmp_path) == original
        assert runtime.calls == 1


def test_failed_polish_does_not_start_a_rewrite_loop(tmp_path):
    runtime = Rewriter("Utilize the result.")
    assert present(runtime, ["Utilize the result."], tmp_path) == "Utilize the result."
    assert runtime.calls == 1


def test_clarity_pass_handles_short_but_abstract_sentences(tmp_path):
    original = "The arbiter applies a repair gate."
    assert alerts(original) == []
    runtime = Rewriter("The harness checks the evidence before it changes the code.")
    assert present(runtime, [original], tmp_path) == runtime.text


def test_meaning_change_falls_back_without_another_rewrite(tmp_path):
    original = "Reject the request when its schema is missing."
    runtime = Rewriter("Accept the request when its schema is missing.", equivalent=False)
    assert present(runtime, [original], tmp_path) == original
    assert runtime.calls == 1
