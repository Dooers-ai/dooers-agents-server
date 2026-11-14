from dooers.agents.server.persistence.sanitize import without_nul


def test_without_nul_strips_nested_strings():
    cleaned = without_nul(
        {
            "result": {"output": "hello\x00world"},
            "args": ["a\x00", {"cmd": "ls\x00"}],
        }
    )
    assert cleaned == {
        "result": {"output": "helloworld"},
        "args": ["a", {"cmd": "ls"}],
    }
    assert "\x00" not in str(cleaned)
