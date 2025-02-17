# Anwendung des Templates

Das Vorgehen unterscheidet sich von dem normalen git clone Vorgehen.
1. Es wird der Zielordner angelegt. Hierin wird das neue Projekt erzeugt.
2. Da wir in VS Code entwickeln, sollte man die installierten Extensions prüfen. Zu installieren sind:
    + black formatter
    + isort
    + python with pylance
    + ruff
3. Folgender Befehl soll im Terminal ausgeführt werden: 
    ```bash
    copier copy git+https://USERNAME@bitbucket.org/geowerkstatt-hamburg/python_project_template PROJKT_ORDNER --trust
    ```
4. Im folgenden Interviewleitfaden können die Einstellungen für das Projekt vorgenommen werden.
5. Update eines vorhandenen Projekts im vorhandenen Projektordner, mit:
    ```bash
    copier update . --trust
    ```

Beispiele für Updates sind bspw. die Änderung der Python-Version.

## sharing data between git-project and "G://" under Windows
### sharing Network folder from VSCode-Server with personal PCs

- klick "Netzwerkadresse hinzufügen"

![network](img/Anmerkung-2023-09-11-141117.png)

- folgende URL eingeben: `\\gv-srv-w00176.fhhnet.stadt.hamburg.de\Shared$`

![url](img/Anmerkung-2023-09-11-141439.png)

## Daten vom Ordner auf der jeweiligen Maschine in den Shared$/Projektordner überführen

Vorgehen:
* rechter Mausklick auf die jeweilige Datei ... Download auswählen
* Zielordner auswählen ... bestätigen
* Dateiendungen prüfen

Falls mehrere Dateien heruntergeladen werden sollen, kann es hilfreich sein, vorher die Dateien zu zippen

    ```bash
    Beispiel für das Zippen
    zip zieldateiname zu_zippende_dateien, z.B. zip data/month_2_3_4_5.zip  data/*.xlsx
    ```
## Hinweise für die Installation und Nutzung

### Vorinstallierte Ordner und Dateien

**Ordner**:
- `assets`: für Abbildungen, Tabellen
- `data`: für lokal hinterlegte Daten
- `reports`: für Dokumentationen
- `examples`: Ordner für einfache Beispiele
- `scratch`: Hier kann man kleine Codesnippets ausprobieren und testen.
- `secrets`: für das Speichern von Passwörten und Zugängen als bspw. Textdateien
- `scripts`
- `src`: für das Package
-`.vscode`: hier ist die settings.json hinterlegt mit den verwendeten Lintern und Grundformatierungen (black, isort)

**weitere Dateien**:
- zur Erstellung von Dockerimages:
    * `Makefile`
    * `Dockerfile`: zur Definition des Images
    * `docker-compose-build.yaml` : für die automatisierte Erstellung des Images aus dem Dockerfile
    * `docker-compose-dev.yaml`: für den lokalen Test des Images
    * `docker-compose.yaml`: für den produktiven Dienst
- für die virtuelle Environment/Umgebung:
    * `environmental.yaml`: diese muss mit dem entsprechenden Befehl initialisiert werden
- `.env.example`: Beispieldatei für die Umgebungsvariablen; Sie beziehen sich auf das Programmierte als auch auf den build-Prozess für das Dockerimage.
- `.gitignore`: alles, was nicht in git synchronisiert werden soll. Hierzu zählt u.a. der assets-Ordner, da hier auch größere Dateien erzeugt werden können. Die Dateien werden in einen separten Prozess auf den Jumpserver geladen und mit den entsprechenden Gruppenlaufwerken synchronisiert.
- `.dockerignore`: nur für den Imageerstellungsprozess relevante Daten werden hier verwendet.
- `pyproject.toml`: alle specs enthalten; build-backend

## Umgang mit den vorgegebenen Konzepten
### virtual environemtns / dependencies:
    * `mamba` zum managengen von venvs und zum installieren von Paketen, v.a. für data science
    * `poetry` zum installieren von paketen und Projekt-Abhängigkeiten

Create an ` environemt.yml` with minimal dependencies

```yaml
name: python_project_template
channels:
  - conda-forge
dependencies:
  - python=3.11
  - poetry

```

```bash
mamba create -p ./.venv python=3.11
```

```bash
conda activate .venv/
```

```bash
mamba env update --prefix ./.venv --file environment.yml  --prune
```

#### How To use poetry to install dependencies

Add a package to your depdendency list (updates pyproject.toml) and install it in your environment
```bash
# add a dependency and install it, with
poetry add PACKAGE
```

```bash
# install your package in editable mode, into your environment
poetry install
```

### Anmerkung für die Anwendung von docker:
    * docker images sind, zur Zeit, die Zielumgebung für fertiggestellte Anwendungen
    * Ziele für den build-Vorgang:
        - möglichst schneller build prozess
        - möglichst kleine images, um deployment nicht unnötig zu verlangsamen
        - hoher (Teil)Automatisierungsgrad
        - Abstraktion der docker-spezifischen Befehle durch Nutzung von makefiles
    * Dev-Container bieten Potentiale für transportable Dev-Umgebungen, die auf allen unseren Entwicklungsmaschinen eingesetzt werden können, ohne


### Anmerkungen für die Codedokumentation

TODO Andreas:     
    * ich schlage vor wir benutzen Jupyterbook für die Doku
    * funtionierendes setup inclusive API Dokumentation (`autoapi`) hier: https://bitbucket.org/geowerkstatt-hamburg/vhh_e_dispo/src/dev/jbdocs/_config.yml

entsprechende Ordner für jbdocs-Anwendung anlegen
notwendige Extensions erwähnen
in Makefile integrieren?
auf vm veröffentlichen? --> wenn Doku da ist, Prozess anpassen

Abspeicherung: als html, latex, doc und/oder pdf?
