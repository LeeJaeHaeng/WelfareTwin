from welfaretwin.data.stats import parse_table

FIXTURE = """
<html><script>var x = "<tr><td>무시</td></tr>";</script>
<table>
<tr><th></th><th>2020</th><th>2021</th><th>2022</th></tr>
<tr><th>수급자 수</th><td>2,134</td><td>2,360</td><td>-</td></tr>
<tr><th>수급률</th><td>4.1</td><td>4.6</td><td>4.8</td></tr>
<tr><th>기 타</th><td>172,256</td><td>212,952</td><td>243,264</td></tr>
</table></html>
"""


def test_parse_table():
    years, data = parse_table(FIXTURE)
    assert years == [2020, 2021, 2022]
    assert data["수급자수"] == [2134.0, 2360.0, None]
    assert data["수급률"] == [4.1, 4.6, 4.8]
    assert data["기타"][2] == 243264.0
