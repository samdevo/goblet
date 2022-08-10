import os

from goblet.backends.backend import Backend
from goblet.client import VersionedClients, get_default_project, get_default_location, get_default_project_number
from goblet.common_cloud_actions import (
    create_cloudbuild,
    destroy_cloudrun,
    destroy_cloudfunction_artifacts,
)
from goblet.config import GConfig
from goblet.revision import RevisionSpec
from goblet.utils import get_dir
from goblet.write_files import write_dockerfile


class CloudRun(Backend):
    resource_type = "cloudrun"
    supported_versions = ["v2"]

    def __init__(self, app, config={}):
        self.client = VersionedClients(app.client_versions).run
        if app.client_versions["run"] == "v2":
            self.run_name = f"projects/{get_default_project()}/locations/{get_default_location()}/services/{app.function_name}"
        else:
            self.run_name = f"namespaces/{get_default_project()}/services/{app.function_name}"
        super().__init__(app, self.client, self.run_name, config=config)

    def deploy(self, force=False, config=None):
        versioned_clients = VersionedClients(self.app.client_versions)
        if config:
            self.config = GConfig(config=config)
        put_headers = {
            "content-type": "application/zip",
        }

        if not os.path.exists(get_dir() + "/Dockerfile") and not os.path.exists(
            get_dir() + "/Procfile"
        ):
            self.log.info(
                "No Dockerfile or Procfile found for cloudrun backend. Writing default Dockerfile"
            )
            write_dockerfile()
        self._zip_file("Dockerfile")

        source = self._gcs_upload(
            self.client,
            put_headers,
            upload_client=versioned_clients.run_uploader,
            force=force,
        )

        self.create_build(versioned_clients.cloudbuild, source, self.name, config)
        serviceRevision = RevisionSpec(config, versioned_clients, self.name)
        serviceRevision.deployRevision()


        # Set IAM Bindings
        if self.config.bindings:
            self.log.info(f"adding IAM bindings for cloudrun {self.name}")
            policy_bindings = {"policy": {"bindings": self.config.bindings}}
            self.client.execute(
                "setIamPolicy",
                parent_key="resource",
                parent_schema=self.run_name,
                params={"body": policy_bindings},
            )

    def destroy(self, all=False):
        destroy_cloudrun(self.client, self.name)
        if all:
            destroy_cloudfunction_artifacts(self.name)

    def create_build(self, client, source=None, name="goblet", config={}):
        """Creates http cloudbuild"""
        if config:
            self.config = GConfig(config=config)
        build_configs = self.config.cloudbuild or {}
        registry = (
            build_configs.get("artifact_registry")
            or f"{get_default_location()}-docker.pkg.dev/{get_default_project()}/cloud-run-source-deploy/{name}"
        )
        build_configs.pop("artifact_registry", None)

        if build_configs.get("serviceAccount") and not build_configs.get("logsBucket"):
            build_options = build_configs.get("options", {})
            if not build_options.get("logging"):
                build_options["logging"] = "CLOUD_LOGGING_ONLY"
                build_configs["options"] = build_options
                self.log.info(
                    "service account given but no logging bucket so defaulting to cloud logging only"
                )

        req_body = {
            "source": {"storageSource": source["storageSource"]},
            "steps": [
                {
                    "name": "gcr.io/cloud-builders/docker",
                    "args": ["build", "-t", registry, "."],
                }
            ],
            "images": [registry],
            **build_configs,
        }

        create_cloudbuild(client, req_body)

        # Set IAM Bindings
        if self.config.bindings:
            self.log.info(f"adding IAM bindings for cloudrun {self.name}")
            policy_bindings = {"policy": {"bindings": self.config.bindings}}
            client.run.execute(
                "setIamPolicy",
                parent_key="resource",
                parent_schema=self.run_name,
                params={"body": policy_bindings},
            )
