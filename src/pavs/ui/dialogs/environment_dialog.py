"""Add / edit a Polarion environment."""
from __future__ import annotations

from pydantic import ValidationError
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout, QWidget)

from ...domain.enums import AuthMethod, EnvironmentKind, ProviderKind
from ...domain.models import Credentials, Environment, TlsSettings

AUTH_LABELS = {
    AuthMethod.TOKEN: "Personal Access Token (REST + SOAP)",
    AuthMethod.PASSWORD: "Username + password (SOAP + SVN; REST unavailable)",
    AuthMethod.TOKEN_AND_PASSWORD: "Token + password (all providers)",
    AuthMethod.NONE: "None (version detection only)",
}


class EnvironmentDialog(QDialog):
    def __init__(self, env: Environment | None, has_secret: dict[str, bool] | None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit environment" if env else "Add environment")
        self.setMinimumWidth(560)
        self._env = env
        has_secret = has_secret or {}
        form = QFormLayout()

        self.name = QLineEdit(env.name if env else "")
        self.name.setPlaceholderText("e.g. Development")
        self.url = QLineEdit(env.base_url if env else "")
        self.url.setPlaceholderText("https://polarion-dev.example.local/polarion")
        self.kind = QComboBox()
        for k in EnvironmentKind:
            self.kind.addItem(k.value.capitalize(), k)
        if env:
            self.kind.setCurrentIndex(list(EnvironmentKind).index(env.kind))

        self.auth = QComboBox()
        for m, label in AUTH_LABELS.items():
            self.auth.addItem(label, m)
        if env:
            self.auth.setCurrentIndex(list(AUTH_LABELS).index(env.auth_method))
        self.username = QLineEdit(env.username or "" if env else "")
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.Password)
        self.token.setPlaceholderText("stored" if has_secret.get("token") else "paste token")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("stored" if has_secret.get("password") else "")

        self.verify_tls = QCheckBox("Verify TLS certificates")
        self.verify_tls.setChecked(env.tls.verify if env else True)
        self.ca_bundle = QLineEdit(env.tls.ca_bundle or "" if env else "")
        self.ca_bundle.setPlaceholderText("optional corporate CA bundle (.pem)")
        browse = QPushButton("Browse")
        browse.clicked.connect(self._pick_ca)
        ca_row = QHBoxLayout()
        ca_row.addWidget(self.ca_bundle)
        ca_row.addWidget(browse)
        ca_w = QWidget()
        ca_w.setLayout(ca_row)
        ca_row.setContentsMargins(0, 0, 0, 0)

        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(3, 600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(env.timeout_seconds if env else 30)

        enabled = set(env.providers_enabled) if env else {ProviderKind.REST, ProviderKind.SOAP}
        self.p_rest = QCheckBox("REST API")
        self.p_soap = QCheckBox("SOAP Web Services")
        self.p_svn = QCheckBox("SVN configuration reader (read-only, opt-in)")
        for box, kind in ((self.p_rest, ProviderKind.REST), (self.p_soap, ProviderKind.SOAP),
                          (self.p_svn, ProviderKind.SVN)):
            box.setChecked(kind in enabled)
        prov = QVBoxLayout()
        for b in (self.p_rest, self.p_soap, self.p_svn):
            prov.addWidget(b)
        prov_w = QWidget()
        prov_w.setLayout(prov)
        prov.setContentsMargins(0, 0, 0, 0)

        self.test_projects = QLineEdit(", ".join(env.test_projects) if env else "")
        self.test_projects.setPlaceholderText("project ids where test data may be created (comma separated)")

        form.addRow("Name", self.name)
        form.addRow("Polarion URL", self.url)
        form.addRow("Environment type", self.kind)
        form.addRow("Authentication", self.auth)
        form.addRow("Username", self.username)
        form.addRow("Personal Access Token", self.token)
        form.addRow("Password", self.password)
        form.addRow("", self.verify_tls)
        form.addRow("CA bundle", ca_w)
        form.addRow("Timeout", self.timeout)
        form.addRow("Providers", prov_w)
        form.addRow("Test projects", self.test_projects)

        note = QLabel("Secrets go to Windows Credential Manager, never to the database or logs. "
                      "Leave a secret field empty to keep the stored value.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(note)
        lay.addWidget(buttons)
        self.auth.currentIndexChanged.connect(self._sync)
        self.kind.currentIndexChanged.connect(self._sync)
        self._sync()
        self.result_env: Environment | None = None
        self.result_creds: Credentials | None = None

    def _pick_ca(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "CA bundle", "", "Certificates (*.pem *.crt *.cer);;All (*)")
        if path:
            self.ca_bundle.setText(path)

    def _sync(self) -> None:
        m = self.auth.currentData()
        self.token.setEnabled(m in (AuthMethod.TOKEN, AuthMethod.TOKEN_AND_PASSWORD))
        self.password.setEnabled(m in (AuthMethod.PASSWORD, AuthMethod.TOKEN_AND_PASSWORD))
        self.username.setEnabled(m != AuthMethod.NONE)
        prod = self.kind.currentData() == EnvironmentKind.PRODUCTION
        self.verify_tls.setEnabled(not prod)
        if prod:
            self.verify_tls.setChecked(True)

    def _accept(self) -> None:
        providers = [k for b, k in ((self.p_rest, ProviderKind.REST), (self.p_soap, ProviderKind.SOAP),
                                    (self.p_svn, ProviderKind.SVN)) if b.isChecked()]
        if not providers:
            QMessageBox.warning(self, "Providers", "Enable at least one provider.")
            return
        data = dict(
            name=self.name.text(), base_url=self.url.text(), kind=self.kind.currentData(),
            auth_method=self.auth.currentData(), username=self.username.text().strip() or None,
            tls=TlsSettings(verify=self.verify_tls.isChecked(), ca_bundle=self.ca_bundle.text().strip() or None),
            timeout_seconds=self.timeout.value(), providers_enabled=providers,
            test_projects=[p.strip() for p in self.test_projects.text().split(",") if p.strip()],
        )
        try:
            env = self._env.model_copy(update=data) if self._env else Environment(**data)
            env = Environment.model_validate(env.model_dump())   # re-run validators on edits
        except ValidationError as exc:
            msgs = "\n".join(f"- {e['loc'][0]}: {e['msg']}" for e in exc.errors())
            QMessageBox.warning(self, "Check the form", msgs)
            return
        if not env.tls.verify:
            if QMessageBox.question(self, "TLS verification off",
                                    "Certificates will not be checked for this environment. Continue?") \
                    != QMessageBox.Yes:
                return
        self.result_env = env
        self.result_creds = Credentials(token=self.token.text().strip() or None,
                                        password=self.password.text() or None)
        self.accept()
