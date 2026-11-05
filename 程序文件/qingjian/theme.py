STYLE = """
* { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif; }
QMainWindow { background: #f5f6fb; }
QDialog { background: #f5f6fb; }
QWidget#workspace, QWidget#jobPage { background: #f5f6fb; }
QWidget { color: #333c52; font-size: 13px; }
QFrame#pageNavigation { background: transparent; border: none; }
QPushButton#pageBack {
    background: #eeecfc; border: 1px solid #dcd6f7; color: #564add;
    border-radius: 10px; padding: 6px 10px; font-size: 12px;
}
QPushButton#pageBack:hover { background: #e6e1fa; border-color: #bfb4ee; }
QPushButton#pageBack:disabled { background: #f1f2f7; color: #a0a8b9; border-color: #e5e8f0; }
QLabel#navigationParent { color: #8790a3; font-size: 12px; }
QFrame#sidebar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fafbfe);
    border: none; border-right: 1px solid #e6e9f2;
}
QFrame#compactToolbar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fafbfe);
    border: none; border-bottom: 1px solid #e6e9f2;
}
QWidget#navBody, QScrollArea#navScroll { background: transparent; }
QLabel#brandMark {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #5e54e6, stop:1 #8267ec);
    color: #ffffff; border-radius: 13px; font-size: 23px; font-weight: 700;
}
QLabel#brand { color: #1e2438; font-size: 18px; font-weight: 700; }
QLabel#brandSub { color: #8790a3; font-size: 10px; }
QLabel#navGroup { color: #919ab0; font-size: 11px; font-weight: 600; padding: 12px 11px 4px; }
QLabel#sideFoot { color: #949cb0; font-size: 11px; padding: 0px 10px; }
QPushButton#notebookLaunch {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6358e6, stop:1 #8b6ee8);
    border: 1px solid #7363e6; color: #ffffff; border-radius: 16px;
    font-size: 14px; font-weight: 600; padding: 10px 12px; text-align: center;
}
QPushButton#notebookLaunch:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6f64ef, stop:1 #9579f0);
    border-color: #8c7cf2; color: #ffffff;
}
QPushButton#notebookLaunch:pressed { background: #5646cf; border-color: #5646cf; color: #ffffff; }
QPushButton#notebookLaunch:focus { border: 2px solid #bfb5ff; }
QPushButton#converterLaunch {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f3f1fd, stop:1 #ece8fb);
    border: 1px solid #d4cdf2; color: #584ab5;
    border-radius: 16px; font-size: 14px; font-weight: 600; padding: 10px 12px;
}
QPushButton#converterLaunch:hover { background: #e7e2fa; border-color: #b1a4e4; color: #4638a3; }
QPushButton#converterLaunch:pressed { background: #ddd6f6; }
QPushButton#converterLaunch:focus { border: 2px solid #a596dc; }
QPushButton#surfaceNav {
    color: #657086; background: transparent; border: 1px solid transparent; text-align: left;
    padding: 11px 16px; border-radius: 14px; font-size: 14px;
}
QPushButton#surfaceNav:hover { background: #f3f2fc; color: #5b50e6; }
QPushButton#surfaceNav:checked {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #eeecfc, stop:1 #f5f3fe);
    color: #564add; border: 1px solid #dcd6f7; font-weight: 600;
}
QPushButton#nav {
    color: #6c7589; background: transparent; border: none; text-align: left;
    padding: 9px 12px; border-radius: 12px; font-size: 13px;
}
QPushButton#nav:hover { background: #f3f3fb; color: #454ec7; }
QPushButton#nav:checked { background: #eeecfc; color: #5c50e6; font-weight: 600; }
QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #1f263c; }
QLabel#subTitle { color: #788296; font-size: 12px; }
QLabel#badge {
    background: #e8f5ee; color: #3c7a63; border: 1px solid #d1ebdd;
    border-radius: 12px; padding: 5px 11px; font-size: 11px; font-weight: 600;
}
QFrame#card, QFrame#actionBar {
    background: #ffffff; border: 1px solid #dde2ec; border-radius: 16px;
}
QFrame#sourceBar {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #ffffff, stop:1 #fcfcfe);
    border: 1px solid #dadfeb; border-radius: 16px;
}
QFrame#sourceBar:hover { border-color: #c8cee0; }
QFrame#sourceBar[dragActive="true"] { background: #f0eeff; border: 1px solid #8e84ee; }
QLabel#sourceMark {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #f2efff, stop:1 #e8e3fe);
    color: #7b6fd6; border-radius: 14px; font-size: 25px; font-weight: 400;
}
QLabel#sourceTitle { color: #3e4862; font-size: 13px; font-weight: 600; }
QLabel#countBadge {
    background: #f0f2f8; color: #727d93; border-radius: 8px;
    padding: 2px 7px; font-size: 10px; font-weight: 600;
}
QLabel#sourceSummary { color: #848ea2; font-size: 11px; }
QLabel#cardTitle { font-size: 16px; font-weight: 600; color: #2b3550; }
QLabel#muted { color: #7e889b; font-size: 12px; }
QLabel#notice {
    background: #f7f8fc; color: #6d7a93; border: 1px solid #e4e8f2;
    border-radius: 12px; padding: 10px 12px; font-size: 12px;
}
QPushButton {
    background: #ffffff; border: 1px solid #dce1eb; border-radius: 11px;
    padding: 8px 14px; color: #56617b;
}
QPushButton:hover { background: #f5f4ff; border-color: #c4bef0; color: #4849cc; }
QPushButton:pressed { background: #eceafe; }
QPushButton:focus { border-color: #9e96ec; }
QPushButton:disabled { color: #bcc2cf; background: #f7f8fb; border-color: #eceef4; }
QPushButton#quiet {
    background: transparent; border: 1px solid transparent;
    padding: 7px 10px; color: #828c9f; font-size: 12px;
}
QPushButton#quiet:hover, QPushButton#quiet:checked { color: #5458d4; background: #f0effc; border-color: #e4e1fa; }
QPushButton#quiet:pressed { background: #e6e4f9; }
QPushButton#quiet:disabled { color: #c3c8d5; background: transparent; border-color: transparent; }
QPushButton#sourceAdd {
    background: #f2f0ff; color: #565cd4; border-color: #e2dffb;
    font-size: 12px; font-weight: 600; padding: 8px 14px;
}
QPushButton#sourceAdd:hover { background: #e8e5ff; border-color: #c9c3f6; color: #464bc6; }
QPushButton#sourceAdd:pressed { background: #ddd8fd; }
QPushButton#sourceAdd:disabled { color: #b1b8d0; background: #f7f7fc; border-color: #eff0f7; }
QPushButton#primary {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6358e6, stop:1 #7764ea);
    color: white; border-color: #6358e6; border-radius: 14px;
    font-size: 14px; font-weight: 600; padding: 11px 26px;
}
QPushButton#primary:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #564add, stop:1 #6a56e2);
    border-color: #564add;
}
QPushButton#primary:pressed { background: #4c40ca; border-color: #4c40ca; }
QPushButton#primary:disabled { background: #bcbff0; border-color: #bcbff0; color: #ffffff; }
QPushButton#danger { color: #b65960; border-color: #edd5d8; background: #fffafa; }
QPushButton#danger:hover { background: #fff0f1; border-color: #e2b4b9; color: #9e4148; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QDateEdit, QTimeEdit, QDateTimeEdit {
    background: #ffffff; border: 1px solid #dce1eb; border-radius: 10px; padding: 7px 9px;
    selection-background-color: #e4e1ff; selection-color: #3b38b5;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QPlainTextEdit:hover,
QDateEdit:hover, QTimeEdit:hover, QDateTimeEdit:hover {
    border-color: #c7cede;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QComboBox:focus,
QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus {
    border-color: #968fe8; background: #ffffff;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    background: #f7f8fb; color: #a0a7b7; border-color: #eaecf3;
}
QLineEdit#outputPath {
    border: 1px solid #eceef5; background: #f6f7fb; color: #8790a4;
    padding: 6px 10px; font-size: 11px; border-radius: 9px;
}
QComboBox::drop-down { border: none; width: 24px; }
QComboBox QAbstractItemView {
    background: #ffffff; border: 1px solid #dfe3ee; border-radius: 10px;
    padding: 4px; selection-background-color: #efedff; selection-color: #4f46c8; outline: 0;
}
QCheckBox { spacing: 9px; padding: 4px 0px; color: #465068; }
QTabWidget::pane {
    border: 1px solid #e4e8f1; border-top: none; background: white;
    border-bottom-left-radius: 14px; border-bottom-right-radius: 14px;
}
QTabBar::tab {
    color: #7c879b; background: transparent; padding: 10px 17px;
    border-bottom: 2px solid #eff1f7;
}
QTabBar::tab:hover { color: #564add; background: #faf9ff; }
QTabBar::tab:selected { color: #5c50e6; border-bottom: 2px solid #7f74ec; font-weight: 600; }
QTableWidget {
    border: 1px solid #e1e5ef; border-radius: 12px; background: white;
    alternate-background-color: #fafbfe;
    selection-background-color: #eeecff; selection-color: #474ec2; gridline-color: #eceff5;
    outline: 0;
}
QHeaderView::section {
    border: none; border-bottom: 1px solid #e1e5ef; color: #6e7b94;
    background: #f8f9fc; padding: 8px; font-weight: 600;
}
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #f2f4f8; }
QTableWidget::item:hover { background: #f6f5fe; }
QListWidget#redactionGroups {
    background: #ffffff; border: 1px solid #e4e7f0; border-radius: 12px; padding: 5px;
    selection-background-color: #efedff; selection-color: #5c50e6; outline: 0;
}
QListWidget#redactionGroups::item { padding: 10px 8px; border-radius: 9px; }
QListWidget#redactionGroups::item:hover { background: #f6f5fd; }
QListWidget#redactionGroups::item:selected { background: #efedff; color: #5c50e6; }
QProgressBar { border: none; border-radius: 3px; background: #edeff6; text-align: center; }
QProgressBar::chunk {
    border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #685ee8, stop:1 #8e78ee);
}
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #dfe3ec; border-radius: 4px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #c4cbd9; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 2px; }
QScrollBar::handle:horizontal { background: #dfe3ec; border-radius: 4px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #c4cbd9; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }
QMenu { background: #ffffff; border: 1px solid #e2e6f0; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 7px 18px 7px 14px; border-radius: 8px; color: #3d465d; }
QMenu::item:selected { background: #efedfc; color: #4c42c6; }
QMenu::separator { height: 1px; background: #eceff5; margin: 5px 8px; }
QToolTip { background: #333c54; color: #ffffff; border: none; border-radius: 8px; padding: 7px 9px; }
"""

