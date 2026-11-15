from app.services import folder_paths as fp


def test_windows_folders_match_ignoring_case_and_slash_direction():
    root = r"C:\Users\Alex\media studio"
    assert fp.folder_contains(root, r"c:\users\alex\media studio")
    assert fp.folder_contains(root, "C:/Users/Alex/media studio/scripts/final")
    assert fp.folder_contains(root + "\\", r"C:\Users\Alex\media studio")
    assert not fp.folder_contains(root, r"C:\Users\Alex\media studio-old")
    assert not fp.folder_contains(root, r"D:\Users\Alex\media studio")


def test_windows_and_posix_folders_never_match_each_other():
    assert not fp.folder_contains(r"C:\work\repo", "/work/repo")
    assert not fp.folder_contains("/work/repo", r"C:\work\repo")


def test_posix_folders_stay_case_sensitive_and_exact():
    assert fp.folder_contains("/home/me/repo", "/home/me/repo/src")
    assert not fp.folder_contains("/home/me/repo", "/home/me/Repo")
    assert not fp.folder_contains("/home/me/repo", "/home/me/repo2")
    assert fp.folder_contains("/", "/home/me")


def test_drive_root_contains_its_folders():
    assert fp.folder_contains("C:\\", r"C:\work")


def test_absolute_and_normalize():
    assert fp.is_absolute_folder(r"C:\Users\me\repo") and fp.is_absolute_folder("C:/Users/me")
    assert fp.is_absolute_folder(r"\\server\share\repo") and fp.is_absolute_folder("/srv/repo")
    assert not fp.is_absolute_folder("repo") and not fp.is_absolute_folder(r"C:repo") and not fp.is_absolute_folder("")
    assert fp.normalize_folder("c:/Users/me/./repo/") == r"C:\Users\me\repo"
    assert fp.normalize_folder("/srv/a/../repo/") == "/srv/repo"
