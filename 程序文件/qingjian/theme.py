STYLE = """
* { font-family: 'Microsoft YaHei UI', 'Microsoft YaHei', sans-serif; }
QMainWindow { background: #f7f7fa; }
QDialog { background: #f7f7fa; }
QWidget#workspace, QWidget#jobPage { background: #f7f7fa; }
QWidget { color: #3a4358; font-size: 13px; }
QFrame#sidebar { background: #fdfdfe; border: none; border-right: 1px solid #eceef4; }
QFrame#compactToolbar { background: #fdfdfe; border: none; border-bottom: 1px solid #eceef4; }
QWidget#navBody, QScrollArea#navScroll { background: #fdfdfe; }
QLabel#brandMark { background: #6a5fe8; color: #ffffff; border-radius: 13px; font-size: 23px; font-weight: 700; }
QLabel#brand { color: #20263b; font-size: 18px; font-weight: 700; }
QLabel#brandSub { color: #8f97a8; font-size: 10px; }
QLabel#navGroup { color: #9aa2b2; font-size: 11px; padding: 12px 11px 4px; }
QLabel#sideFoot { color: #9aa1b1; font-size: 11px; padding: 0px 10px; }
QPushButton#notebookLaunch {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6a5fe8,stop:1 #8f72e6);
    border: 1px solid #7a68e8; color: #ffffff; border-radius: 16px;
    font-size: 14px; font-weight: 600; padding: 10px 12px; text-align: center;
}
QPushButton#notebookLaunch:hover { background: #7566ec; border-color: #9384f5; color: #ffffff; }
QPushButton#notebookLaunch:pressed { background: #5c4bd3; border-color: #5c4bd3; color: #ffffff; }
QPushButton#notebookLaunch:focus { border: 2px solid #c1b7ff; }
QPushButton#converterLaunch { background: #f1effc; border: 1px solid #d6cff2; color: #5e50b9;
    border-radius: 16px; font-size: 14px; font-weight: 600; padding: 10px 12px; }
QPushButton#converterLaunch:hover { background: #e9e5fa; border-color: #b4a8e4; color: #4b3da6; }
QPushButton#converterLaunch:focus { border: 2px solid #a596dc; }
QPushButton#surfaceNav { color: #6c768b; background: transparent; border: none; text-align: left;
                         padding: 11px 16px; border-radius: 14px; font-size: 14px; }
QPushButton#surfaceNav:hover { background: #f5f4fc; color: #6257ed; }
QPushButton#surfaceNav:checked { background: #f0eefc; color: #5d52df; border: 1px solid #e0dbf6; font-weight: 600; }
QPushButton#nav { color: #737b8e; background: transparent; border: none; text-align: left;
                  padding: 9px 12px; border-radius: 12px; font-size: 13px; }
QPushButton#nav:hover { background: #f5f5fb; color: #3e4dc2; }
QPushButton#nav:checked { background: #f0eefc; color: #6257ed; font-weight: 600; }
QLabel#pageTitle { font-size: 26px; font-weight: 700; color: #232a40; }
QLabel#subTitle { color: #7f889b; font-size: 12px; }
QLabel#badge { background: #ebf6f0; color: #46826b; border-radius: 12px; padding: 5px 11px; font-size: 11px; }
QFrame#card, QFrame#actionBar { background: #ffffff; border: 1px solid #dce0e9; border-radius: 16px; }
QFrame#sourceBar { background: #ffffff; border: 1px solid #dce0e9; border-radius: 16px; }
QFrame#sourceBar[dragActive="true"] { background: #f2f1ff; border: 1px solid #a9a1f0; }
QLabel#sourceMark { background: #f1eeff; color: #8c80d9; border-radius: 14px; font-size: 25px; font-weight: 400; }
QLabel#sourceTitle { color: #48516a; font-size: 13px; font-weight: 600; }
QLabel#countBadge { background: #f3f4f8; color: #7f899e; border-radius: 8px; padding: 2px 7px; font-size: 10px; }
QLabel#sourceSummary { color: #8a93a5; font-size: 11px; }
QLabel#cardTitle { font-size: 16px; font-weight: 600; color: #313b55; }
QLabel#muted { color: #828b9d; font-size: 12px; }
QLabel#notice { background: #f8f8fc; color: #748199; border: 1px solid #e6e9f1;
                border-radius: 12px; padding: 10px 12px; font-size: 12px; }
QPushButton { background: #ffffff; border: 1px solid #e0e4ec; border-radius: 11px;
              padding: 8px 14px; color: #5f6a84; }
QPushButton:hover { background: #f6f6ff; border-color: #c9c4f2; color: #4d4fd0; }
QPushButton:pressed { background: #efeefe; }
QPushButton:disabled { color: #bec3d0; background: #f8f8fb; border-color: #eef0f5; }
QPushButton#quiet { background: transparent; border: 1px solid transparent; padding: 7px 10px; color: #8a93a6; font-size: 12px; }
QPushButton#quiet:hover, QPushButton#quiet:checked { color: #5a5fd6; background: #f2f1fc; }
QPushButton#quiet:disabled { color: #c3c8d5; background: transparent; }
QPushButton#sourceAdd { background: #f3f2ff; color: #5d63d6; border-color: #ebe9fc; font-size: 12px; padding: 8px 14px; }
QPushButton#sourceAdd:hover { background: #ebe9ff; border-color: #d0ccf8; }
QPushButton#sourceAdd:disabled { color: #b1b8d0; background: #f7f7fc; border-color: #eff0f7; }
QPushButton#primary { background: #6a5fe8; color: white; border-color: #6a5fe8; border-radius: 14px;
                     font-size: 14px; font-weight: 600; padding: 11px 26px; }
QPushButton#primary:hover { background: #5b4fdc; border-color: #5b4fdc; }
QPushButton#primary:disabled { background: #bcbff0; border-color: #bcbff0; color: #ffffff; }
QPushButton#danger { color: #b3666b; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QDateEdit, QTimeEdit, QDateTimeEdit {
    background: #ffffff; border: 1px solid #e0e4ec; border-radius: 10px; padding: 7px 9px;
    selection-background-color: #e6e4ff; selection-color: #3f3cb9;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QDateEdit:focus, QTimeEdit:focus, QDateTimeEdit:focus { border-color: #a9a3ee; }
QLineEdit:disabled { background: #f8f8fb; color: #a0a7b7; }
QLineEdit#outputPath { border: none; background: #f6f6fa; color: #939bae; padding: 6px 10px; font-size: 11px; }
QComboBox::drop-down { border: none; width: 24px; }
QCheckBox { spacing: 9px; padding: 4px 0px; }
QTabWidget::pane { border: 1px solid #e6e9f0; border-top: none; background: white;
                   border-bottom-left-radius: 14px; border-bottom-right-radius: 14px; }
QTabBar::tab { color: #818c9f; background: transparent; padding: 10px 17px; border-bottom: 2px solid #f1f2f7; }
QTabBar::tab:selected { color: #6257ed; border-bottom: 2px solid #8a80ee; font-weight: 600; }
QTableWidget { border: 1px solid #e3e7ef; border-radius: 12px; background: white; alternate-background-color: #fafafd;
               selection-background-color: #efeeff; selection-color: #4b52c6; gridline-color: #eceff5; }
QHeaderView::section { border: none; border-bottom: 1px solid #e3e7ef; color: #75829a;
                      background: #f9f9fc; padding: 8px; font-weight: 400; }
QTableWidget::item { padding: 5px 8px; border-bottom: 1px solid #f3f4f8; }
QListWidget#redactionGroups { background: #ffffff; border: 1px solid #e7e9f0; border-radius: 12px; padding: 5px;
    selection-background-color: #efedff; selection-color: #6257ed; }
QListWidget#redactionGroups::item { padding: 10px 8px; border-radius: 9px; }
QListWidget#redactionGroups::item:hover { background: #f7f6fd; }
QListWidget#redactionGroups::item:selected { background: #efedff; color: #6257ed; }
QProgressBar { border: none; border-radius: 3px; background: #f0f1f7; text-align: center; }
QProgressBar::chunk { border-radius: 3px; background: #7d77ea; }
QScrollArea { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px; }
QScrollBar::handle:vertical { background: #e1e4ec; border-radius: 2px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #c9cfdd; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
QMenu { background: #ffffff; border: 1px solid #e6e8f0; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 7px 18px 7px 14px; border-radius: 8px; }
QMenu::item:selected { background: #f1effc; color: #4f45c8; }
QMenu::separator { height: 1px; background: #eef0f5; margin: 5px 8px; }
QToolTip { background: #3d465e; color: #ffffff; border: none; border-radius: 8px; padding: 7px 9px; }
"""
