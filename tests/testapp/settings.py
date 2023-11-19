from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "django-testing"
DEBUG = True
INSTALLED_APPS = ["testapp", "django_stator"]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql_psycopg2",
        "NAME": "stator",
    }
}
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
