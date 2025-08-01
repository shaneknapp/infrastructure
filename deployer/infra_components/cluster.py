from __future__ import annotations

import json
import os
import subprocess
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ruamel.yaml import YAML

from deployer.infra_components.hub import Hub
from deployer.utils.env_vars_management import unset_env_vars
from deployer.utils.file_acquisition import (
    CONFIG_CLUSTERS_PATH,
    HELM_CHARTS_DIR,
    get_decrypted_file,
    get_decrypted_files,
)
from deployer.utils.helm import wait_for_deployments_daemonsets
from deployer.utils.jsonnet import render_jsonnet
from deployer.utils.rendering import print_colour

yaml = YAML(typ="rt")


class Cluster:
    """
    A single k8s cluster we can deploy to
    """

    @classmethod
    def from_name(cls, cluster_name: str) -> Cluster:
        cluster_config_path = CONFIG_CLUSTERS_PATH / cluster_name / "cluster.yaml"

        if not cluster_config_path.exists():
            raise FileNotFoundError(f"No cluster named {cluster_name} found")

        with open(cluster_config_path) as f:
            config = yaml.load(f)
        return cls(config, cluster_config_path)

    def __init__(self, spec, config_path: Path):
        self.spec = spec
        self.config_path = config_path
        self.config_dir = config_path.parent
        self.hubs = [Hub(self, hub_spec) for hub_spec in self.spec.get("hubs", [])]
        self.support = self.spec.get("support", {})

    @contextmanager
    def auth(self):
        if self.spec["provider"] == "gcp":
            yield from self.auth_gcp()
        elif self.spec["provider"] == "aws":
            yield from self.auth_aws()
        elif self.spec["provider"] == "azure":
            yield from self.auth_azure()
        elif self.spec["provider"] == "kubeconfig":
            yield from self.auth_kubeconfig()
        else:
            raise ValueError(f'Provider {self.spec["provider"]} not supported')

    def deploy_support(self, cert_manager_version, debug, dry_run):
        cert_manager_url = "https://charts.jetstack.io"

        print_colour("Provisioning cert-manager...")
        subprocess.check_call(
            [
                "kubectl",
                "apply",
                "-f",
                f"https://github.com/cert-manager/cert-manager/releases/download/{cert_manager_version}/cert-manager.crds.yaml",
            ]
        )
        subprocess.check_call(
            [
                "helm",
                "upgrade",
                "cert-manager",  # given release name (aka. installation name)
                "cert-manager",  # helm chart to install
                f"--repo={cert_manager_url}",
                "--install",
                "--create-namespace",
                "--namespace=cert-manager",
                f"--version={cert_manager_version}",
            ]
        )
        print_colour("Done!")

        if self.spec["provider"] == "aws":
            print_colour("Provisioning tigera operator...")
            # Hardcoded here, as we want to upgrade everywhere together
            # Ideally this would be a subchart of our support chart,
            # but helm has made some unfortunate architectural choices
            # with respect to CRDs and they seem super unreliable when
            # used as subcharts. So we install it here directly from the
            # manifests.
            # We unconditionally install this on all AWS clusters - however,
            # that doesn't actually turn NetworkPolicy enforcement on. That
            # requires setting `calico.enabled` to True in `support` so a
            # calico `Installation` object can be set up.
            # I deeply loathe the operator *singleton* pattern.
            tigera_operator_version = "v3.29.3"
            subprocess.check_call(
                [
                    "kubectl",
                    "apply",
                    "--force-conflicts",  # Remove after https://github.com/2i2c-org/infrastructure/issues/5961
                    "--server-side",  # https://github.com/projectcalico/calico/issues/7826
                    "-f",
                    f"https://raw.githubusercontent.com/projectcalico/calico/{tigera_operator_version}/manifests/tigera-operator.yaml",
                ]
            )
            print_colour("Done!")

            # Patch the tigera operator to remove the NoSchedule toleration
            # otherwise it will schedule on tainted nodes
            print_colour("Patching tigera operator...")
            patch_tolerations = {
                "spec": {
                    "template": {
                        "spec": {
                            "tolerations": [
                                {"effect": "NoExecute", "operator": "Exists"},
                            ],
                        }
                    }
                }
            }
            patch_tolerations_json = json.dumps(patch_tolerations)
            subprocess.check_call(
                [
                    "kubectl",
                    "--namespace",
                    "tigera-operator",
                    "patch",
                    "deployment",
                    "tigera-operator",
                    "--patch",
                    patch_tolerations_json,
                ],
            )
            print_colour("Done!")

        print_colour("Provisioning support charts...")

        support_dir = HELM_CHARTS_DIR.joinpath("support")
        subprocess.check_call(["helm", "dep", "up", support_dir])

        # contains both encrypted and unencrypted values files
        values_file_paths = [
            support_dir.joinpath("enc-support.secret.values.yaml"),
            support_dir.joinpath("enc-cryptnono.secret.values.yaml"),
            support_dir.joinpath("values.jsonnet"),
        ] + [self.config_dir / p for p in self.support["helm_chart_values_files"]]

        with (
            get_decrypted_files(values_file_paths) as values_files,
            ExitStack() as jsonnet_stack,
        ):
            cmd = [
                "helm",
                "upgrade",
                "--install",
                "--create-namespace",
                "--namespace=support",
                "support",
                str(support_dir),
            ]

            for values_file in values_files:
                _, ext = os.path.splitext(values_file)
                if ext == ".jsonnet":
                    rendered_path = jsonnet_stack.enter_context(
                        render_jsonnet(Path(values_file), self.spec["name"], None)
                    )
                    cmd.append(f"--values={rendered_path}")
                else:
                    cmd.append(f"--values={values_file}")

            if debug:
                cmd.append("--debug")

            if dry_run:
                cmd.append("--dry-run")

            print_colour(f"Running {' '.join([str(c) for c in cmd])}")
            subprocess.check_call(cmd)

        wait_for_deployments_daemonsets("support")
        print_colour("Done!")

    def auth_kubeconfig(self):
        """
        Context manager for authenticating with just a kubeconfig file

        For the duration of the contextmanager, we:
        1. Decrypt the file specified in kubeconfig.file with sops
        2. Set `KUBECONFIG` env var to our decrypted file path, so applications
           we call (primarily helm) will use that as config
        """
        config = self.spec["kubeconfig"]
        config_path = self.config_dir / config["file"]

        with (
            get_decrypted_file(config_path) as decrypted_key_path,
            unset_env_vars(["KUBECONFIG"]),
        ):
            os.environ["KUBECONFIG"] = decrypted_key_path
            yield

    def auth_aws(self):
        """
        Reads `aws` nested config and temporarily sets environment variables
        like `KUBECONFIG`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY`
        before trying to authenticate with the `aws eks update-kubeconfig` command.

        Finally get those environment variables to the original values to prevent
        side-effects on existing local configuration.
        """
        config = self.spec["aws"]
        key_path = self.config_dir / config["key"]
        cluster_name = config["clusterName"]
        region = config["region"]

        # Unset all env vars that start with AWS_, as that might affect the aws
        # commandline we call. This could make some weird error messages.
        unset_envs = ["KUBECONFIG"] + [k for k in os.environ if k.startswith("AWS_")]

        with tempfile.NamedTemporaryFile() as kubeconfig, unset_env_vars(unset_envs):
            with get_decrypted_file(key_path) as decrypted_key_path:
                decrypted_key_abspath = os.path.abspath(decrypted_key_path)
                if not os.path.isfile(decrypted_key_abspath):
                    raise FileNotFoundError("The decrypted key file does not exist")
                with open(decrypted_key_abspath) as f:
                    creds = json.load(f)

                os.environ["AWS_ACCESS_KEY_ID"] = creds["AccessKey"]["AccessKeyId"]
                os.environ["AWS_SECRET_ACCESS_KEY"] = creds["AccessKey"][
                    "SecretAccessKey"
                ]

            os.environ["KUBECONFIG"] = kubeconfig.name

            subprocess.check_call(
                [
                    "aws",
                    "eks",
                    "update-kubeconfig",
                    f"--name={cluster_name}",
                    f"--region={region}",
                ]
            )

            yield

    def auth_azure(self):
        """
        Read `azure` nested config, login to Azure with a Service Principal,
        activate the appropriate subscription, then authenticate against the
        cluster using `az aks get-credentials`.
        """
        config = self.spec["azure"]
        key_path = self.config_dir / config["key"]
        cluster = config["cluster"]
        resource_group = config["resource_group"]

        with (
            tempfile.NamedTemporaryFile() as kubeconfig,
            unset_env_vars(["KUBECONFIG"]),
        ):
            os.environ["KUBECONFIG"] = kubeconfig.name

            with get_decrypted_file(key_path) as decrypted_key_path:
                decrypted_key_abspath = os.path.abspath(decrypted_key_path)
                if not os.path.isfile(decrypted_key_abspath):
                    raise FileNotFoundError("The decrypted key file does not exist")

                with open(decrypted_key_path) as f:
                    service_principal = json.load(f)

            # Login to Azure
            subprocess.check_call(
                [
                    "az",
                    "login",
                    "--service-principal",
                    f"--username={service_principal['service_principal_id']}",
                    f"--password={service_principal['service_principal_password']}",
                    f"--tenant={service_principal['tenant_id']}",
                ]
            )

            # Set the Azure subscription
            subprocess.check_call(
                [
                    "az",
                    "account",
                    "set",
                    f"--subscription={service_principal['subscription_id']}",
                ]
            )

            # Get cluster creds
            subprocess.check_call(
                [
                    "az",
                    "aks",
                    "get-credentials",
                    f"--name={cluster}",
                    f"--resource-group={resource_group}",
                ]
            )

            yield

    def auth_gcp(self):
        config = self.spec["gcp"]
        key_path = self.config_dir / config["key"]
        project = config["project"]
        # If cluster is regional, it'll have a `region` key set.
        # Else, it'll just have a `zone` key set. Let's respect either.
        location = config.get("zone", config.get("region"))
        cluster = config["cluster"]

        orig_file = os.environ.get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE")
        orig_kubeconfig = os.environ.get("KUBECONFIG")
        try:
            with (
                tempfile.NamedTemporaryFile() as kubeconfig,
                get_decrypted_file(key_path) as decrypted_file,
            ):
                os.environ["KUBECONFIG"] = kubeconfig.name
                os.environ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = decrypted_file

                subprocess.check_call(
                    [
                        "gcloud",
                        "container",
                        "clusters",
                        # --zone works with regions too
                        f"--zone={location}",
                        f"--project={project}",
                        "get-credentials",
                        cluster,
                    ]
                )

                yield
        finally:
            # restore modified environment variables to its previous state
            if orig_kubeconfig is not None:
                os.environ["KUBECONFIG"] = orig_kubeconfig
            else:
                os.environ.pop("KUBECONFIG")
            if orig_file is not None:
                os.environ["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] = orig_file
            else:
                os.environ.pop("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE")

    def get_grafana_url(self) -> str:
        """
        Return full Grafana URL for this cluster.

        Raises an exception if URL is not correctly set.
        """
        config_file = self.config_dir / "support.values.yaml"
        with open(config_file) as f:
            support_config = yaml.load(f)

        grafana_tls_config = (
            support_config.get("grafana", {}).get("ingress", {}).get("tls", [])
        )
        if not grafana_tls_config:
            raise ValueError(
                f'grafana.ingress.tls config for {self.spec["name"]} missing!'
            )

        # We only have one tls host right now. Modify this when things change.
        return "https://" + grafana_tls_config[0]["hosts"][0]

    def get_grafana_token(self) -> str:
        """
        Return access token for talking to the Grafana API on this cluster
        """
        grafana_token_file = self.config_dir / "enc-grafana-token.secret.yaml"

        # Read the secret grafana token file
        with get_decrypted_file(grafana_token_file) as decrypted_file_path:
            with open(decrypted_file_path) as f:
                config = yaml.load(f)

        if "grafana_token" not in config.keys():
            raise ValueError(
                f'Grafana service account token not found, use `deployer new-grafana-token {self.spec["cluster_name"]}`'
            )

        return config["grafana_token"]

    def get_external_prometheus_url(self) -> str:
        """
        Return full Prometheus URL for this cluster.

        Raises an exception if URL is not correctly configured
        """

        config_file = self.config_dir / "support.values.yaml"
        with open(config_file) as f:
            support_config = yaml.load(f)

        # Don't return the address if the prometheus instance wasn't securely exposed to the outside.
        if not support_config.get("prometheusIngressAuthSecret", {}).get(
            "enabled", False
        ):
            raise ValueError(
                f"""`prometheusIngressAuthSecret` wasn't configured for {self.spec["name"]}"""
            )

        tls_config = (
            support_config.get("prometheus", {})
            .get("server", {})
            .get("ingress", {})
            .get("tls", [])
        )

        if not tls_config:
            raise ValueError(
                f'No tls config was found for the prometheus instance of {self.spec["name"]}'
            )

        # We only have one tls host right now. Modify this when things change.
        return f'https://{tls_config[0]["hosts"][0]}'

    def get_cluster_prometheus_creds(self) -> tuple[str, str]:
        """
        Retrieve basic auth credentials for accessing the prometheus instance of this cluster

        Raises an exception if it was not correctly configured
        """
        config_filename = self.config_dir / "enc-support.secret.values.yaml"

        with get_decrypted_file(config_filename) as decrypted_path:
            with open(decrypted_path) as f:
                support_config = yaml.load(f)

        # Don't return the address if the prometheus instance wasn't securely exposed to the outside.
        if "prometheusIngressAuthSecret" not in support_config:
            raise ValueError(
                f"""`prometheusIngressAuthSecret` wasn't configured for {self.spec["name"]}"""
            )

        return (
            support_config["prometheusIngressAuthSecret"]["username"],
            support_config["prometheusIngressAuthSecret"]["password"],
        )
