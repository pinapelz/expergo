from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)


class RipCdDialog(QDialog):
    def __init__(self, drives: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select optical drive")
        self.resize(680, 360)
        self.selected_drive: str | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select the optical drive to use for CD ripping:"))

        self.drives = QListWidget()
        for path, label in drives:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.drives.addItem(item)
        if drives:
            self.drives.setCurrentRow(0)
        self.drives.itemDoubleClicked.connect(lambda _: self._accept())
        layout.addWidget(self.drives, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok_btn = QPushButton("Use Drive")
        ok_btn.clicked.connect(self._accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(ok_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

    def _accept(self):
        item = self.drives.currentItem()
        if not item:
            QMessageBox.information(self, "No drive selected", "Select an optical drive first.")
            return
        self.selected_drive = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
