import logging
from typing import cast

from django.apps import apps
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs a Stator runner"

    def add_arguments(self, parser):
        parser.add_argument(
            "--concurrency",
            "-c",
            type=int,
            default=16,
            help="How many tasks to run at once",
        )
        parser.add_argument(
            "--liveness-file",
            type=str,
            default=None,
            help="A file to touch at least every 30 seconds to say the runner is alive",
        )
        parser.add_argument(
            "--run-for",
            "-r",
            type=int,
            default=0,
            help="How long to run for before exiting (defaults to infinite)",
        )
        parser.add_argument(
            "--exclude",
            "-x",
            type=str,
            action="append",
            help="Model labels that should not be processed",
        )
        parser.add_argument("model_labels", nargs="*", type=str)

    def handle(
        self,
        model_labels: list[str],
        concurrency: int,
        liveness_file: str,
        schedule_interval: int,
        run_for: int,
        exclude: list[str],
        *args,
        **options,
    ):
        # Cache system config
        Config.system = Config.load_system()
        logging.basicConfig(
            format="[%(asctime)s] %(levelname)8s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
            level=logging.INFO,
            force=True,
        )
        # Resolve the models list into names
        models = cast(
            list[type[StatorModel]],
            [apps.get_model(label) for label in model_labels],
        )
        excluded = cast(
            list[type[StatorModel]],
            [apps.get_model(label) for label in (exclude or [])],
        )
        if not models:
            models = StatorModel.subclasses
        models = [model for model in models if model not in excluded]
        logger.info(
            "Running for models: " + " ".join(m._meta.label_lower for m in models)
        )
        # Run a runner
        runner = StatorRunner(
            models,
            concurrency=concurrency,
            liveness_file=liveness_file,
            schedule_interval=schedule_interval,
            run_for=run_for,
        )
        try:
            runner.run()
        except KeyboardInterrupt:
            logger.critical("Ctrl-C received")
