from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QComboBox,
    QInputDialog,
    QMessageBox,
    QMenu,
    QDateEdit,
    QDialog,
    QDialogButtonBox
)

from PyQt6.QtGui import QAction, QKeyEvent
from PyQt6.QtCore import Qt, QDate


class TasksView(QWidget):
    def __init__(self, task_manager):
        super().__init__()

        self.task_manager = task_manager

        self._init_ui()
        self.load_tasks()

    # -------------------------
    # UI SETUP
    # -------------------------

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 20)
        self.setLayout(layout)

        # Header
        header_layout = QHBoxLayout()

        title = QLabel("Tasks")
        title.setStyleSheet("font-size: 24px; font-weight: bold;")

        self.filter_box = QComboBox()
        self.filter_box.addItems(["All", "Active", "Completed", "Due Today"])
        self.filter_box.currentIndexChanged.connect(self.load_tasks)

        self.add_button = QPushButton("Add Task")
        self.add_button.clicked.connect(self.add_task)

        header_layout.addWidget(title)
        header_layout.addStretch()
        header_layout.addWidget(self.filter_box)
        header_layout.addWidget(self.add_button)

        layout.addLayout(header_layout)

        # Task List
        self.task_list = QListWidget()
        self.task_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.task_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.task_list.customContextMenuRequested.connect(self.open_context_menu)

        layout.addWidget(self.task_list)

    # -------------------------
    # LOAD TASKS
    # -------------------------

    def load_tasks(self):
        self.task_list.clear()

        filter_mode = self.filter_box.currentText()
        today = QDate.currentDate()

        all_tasks = self.task_manager.get_all_tasks(include_completed=True)

        # Filter
        if filter_mode == "Active":
            tasks = [t for t in all_tasks if t["completed"] == 0]

        elif filter_mode == "Completed":
            tasks = [t for t in all_tasks if t["completed"] == 1]

        elif filter_mode == "Due Today":
            tasks = []
            for t in all_tasks:
                if t.get("due_date"):
                    due = QDate.fromString(t["due_date"], "yyyy-MM-dd")
                    if due == today and t["completed"] == 0:
                        tasks.append(t)
        else:
            tasks = all_tasks

        # Sort by nearest due date (None dates go last)
        def sort_key(task):
            if task.get("due_date"):
                due = QDate.fromString(task["due_date"], "yyyy-MM-dd")
                return due.toJulianDay()
            return 999999999  # push no-date tasks to bottom

        tasks.sort(key=sort_key)

        for task in tasks:
            self._create_task_widget(task)

    # -------------------------
    # ADD TASK
    # -------------------------

    def add_task(self):
        title, ok = QInputDialog.getText(self, "New Task", "Task title:")
        if not ok or not title.strip():
            return

        # Date picker dialog
        date_dialog = QDateEdit()
        date_dialog.setCalendarPopup(True)
        date_dialog.setDate(QDate.currentDate())

        dialog = QDialog(self)
        dialog.setWindowTitle("Select Due Date")

        layout = QVBoxLayout(dialog)

        date_dialog = QDateEdit()
        date_dialog.setCalendarPopup(True)
        date_dialog.setDate(QDate.currentDate())

        layout.addWidget(date_dialog)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        due_date = date_dialog.date().toString("yyyy-MM-dd")

        try:
            self.task_manager.create_task(
                title=title,
                due_date=due_date,
            )
            self.load_tasks()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    # -------------------------
    # CREATE TASK WIDGET
    # -------------------------

    def _create_task_widget(self, task):
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, task["id"])

        container = QWidget()
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 6, 8, 6)
        container.setLayout(layout)

        # Top Row
        top_row = QHBoxLayout()

        checkbox = QCheckBox(task["title"])
        checkbox.setChecked(bool(task["completed"]))
        checkbox.stateChanged.connect(
            lambda state, task_id=task["id"]: self.toggle_complete(task_id, state)
        )


        top_row.addWidget(checkbox)
        top_row.addStretch()

        layout.addLayout(top_row)

        # Meta info
        meta_parts = [f"Created: {task['created_at']}"]

        today = QDate.currentDate()

        if task.get("due_date"):
            due = QDate.fromString(task["due_date"], "yyyy-MM-dd")
            days_remaining = today.daysTo(due)

            if days_remaining > 1:
                remaining_text = f"{days_remaining} days left"
            elif days_remaining == 1:
                remaining_text = "Due tomorrow"
            elif days_remaining == 0:
                remaining_text = "Due today"
            else:
                remaining_text = f"{abs(days_remaining)} days overdue"

            meta_parts.append(f"Due: {task['due_date']} ({remaining_text})")

        if task["completed"] and task["completed_at"]:
            meta_parts.append(f"Completed: {task['completed_at']}")

        meta = QLabel(" | ".join(meta_parts))
        meta.setStyleSheet("font-size: 12px; color: #AAAAAA;")

        # Highlight overdue
        if task.get("due_date") and not task["completed"]:
            due = QDate.fromString(task["due_date"], "yyyy-MM-dd")
            if due < today:
                meta.setStyleSheet("font-size: 12px; color: #E74C3C;")

        layout.addWidget(meta)

    # -------------------------
    # COMPLETE TOGGLE
    # -------------------------

    def toggle_complete(self, task_id, state):
        if state == Qt.CheckState.Checked.value:
            self.task_manager.mark_completed(task_id)
        self.load_tasks()

    # -------------------------
    # RIGHT CLICK MENU
    # -------------------------

    def open_context_menu(self, position):
        item = self.task_list.itemAt(position)
        if not item:
            return

        task_id = item.data(Qt.ItemDataRole.UserRole)

        menu = QMenu()

        edit_action = QAction("Edit")
        delete_action = QAction("Delete")

        edit_action.triggered.connect(lambda: self.edit_task(task_id))
        delete_action.triggered.connect(lambda: self.delete_task(task_id))

        menu.addAction(edit_action)
        menu.addAction(delete_action)

        menu.exec(self.task_list.mapToGlobal(position))

    # -------------------------
    # DELETE
    # -------------------------

    def delete_task(self, task_id):
        self.task_manager.delete_task(task_id)
        self.load_tasks()

    # -------------------------
    # EDIT
    # -------------------------

    def edit_task(self, task_id):
        tasks = self.task_manager.get_all_tasks(True)
        task = next((t for t in tasks if t["id"] == task_id), None)
        if not task:
            return

        new_title, ok = QInputDialog.getText(
            self, "Edit Task", "Title:", text=task["title"]
        )
        if not ok:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("Edit Due Date")

        layout = QVBoxLayout(dialog)

        date_dialog = QDateEdit()
        date_dialog.setCalendarPopup(True)

        if task.get("due_date"):
            date_dialog.setDate(QDate.fromString(task["due_date"], "yyyy-MM-dd"))
        else:
            date_dialog.setDate(QDate.currentDate())

        layout.addWidget(date_dialog)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_due_date = date_dialog.date().toString("yyyy-MM-dd")

        dialog = QDialog(self)
        dialog.setWindowTitle("Select Due Date")

        layout = QVBoxLayout(dialog)

        date_dialog = QDateEdit()
        date_dialog.setCalendarPopup(True)
        date_dialog.setDate(QDate.currentDate())

        layout.addWidget(date_dialog)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        layout.addWidget(buttons)

        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        new_due_date = date_dialog.date().toString("yyyy-MM-dd")

        self.task_manager.update_task(
            task_id,
            title=new_title,
            due_date=new_due_date,
        )

        self.load_tasks()

    # -------------------------
    # DELETE KEY SUPPORT
    # -------------------------

    def keyPressEvent(self, a0: QKeyEvent | None) -> None:
        if a0 and a0.key() == Qt.Key.Key_Delete:
            selected_items = self.task_list.selectedItems()
            for item in selected_items:
                task_id = item.data(Qt.ItemDataRole.UserRole)
                self.task_manager.delete_task(task_id)
            self.load_tasks()
        else:
            super().keyPressEvent(a0)