from app.routes.dashboard_shared import _after_page_header

ROW = '<nav class="page-tabs">x</nav>'


def test_the_row_goes_directly_under_the_heading_block():
    body = '<div class="page-header"><h1>A</h1><div class="page-actions"><button>b</button></div></div><div class="card">c</div>'
    out = _after_page_header(body, ROW)
    assert out.index("</button></div></div>") < out.index(ROW) < out.index('<div class="card">')


def test_a_header_with_extra_classes_counts_and_no_header_puts_it_on_top():
    assert _after_page_header('<div class="page-header setup-page-header"><h1>A</h1></div>X', ROW).endswith("</div>" + ROW + "X")
    assert _after_page_header("<p>x</p>", ROW).startswith(ROW)
    assert _after_page_header("<p>x</p>", "") == "<p>x</p>"
    assert _after_page_header('<div class="page-header"><div>oops', ROW).startswith(ROW)  # unbalanced: do not guess
