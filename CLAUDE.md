# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this repo is

A personal fork (`origin` = `holywen/terraform-aws-cloudbees-ci-eks-addon`, `upstream` =
`cloudbees-oss/terraform-aws-cloudbees-ci-eks-addon`) of CloudBees's official Terraform
add-on for deploying CloudBees CI on AWS EKS. Working branch: **`keysight-casc-test`**.

The purpose of this fork is **not** feature development on the add-on itself — it's a
live testbed to empirically verify CloudBees support-ticket claims by actually deploying
the scenario and reproducing (or disproving) the reported behavior, rather than trusting
a vendor's written explanation at face value. See `../tickets/` (sibling directory, not
part of this git repo) for the investigation write-ups.

## Repo layout

- `blueprints/01-getting-started/` — simple single-controller blueprint (LDAP realm). Not
  actively used for current testing; last known state: **fully destroyed** (0 resources
  in `terraform.tfstate`).
- `blueprints/02-at-scale/` — the main testing ground. HA/multi-controller blueprint with:
  - `cbci/casc/oc/` — Operations Center CasC bundle (`bundle.yaml`, `rbac.yaml`,
    `items.root.yaml`, `jcasc.*.yaml`, `plugins.yaml`, `variables.yaml`).
  - `cbci/casc/mc/mc-parent/` — shared parent CasC bundle inherited by **all** managed
    controllers (`bundle.yaml`, `rbac.yaml`, `jcasc.*.yaml`, `items.*-folder.yaml`).
  - `.auto.tfvars` — real values (hosted zone, trial license, tags). Gitignored pattern
    exists (`.terraform/*`, state/plan files) but tfvars/state currently live on disk.
- `blueprints/Makefile` — deploy/validate/destroy orchestration (see below).
- `../tickets/` — one directory up, **not tracked in this git repo**. Contains, per ticket:
  a `Test*_Results_*.md` (raw evidence: commands, output, screenshots-in-text) and a
  `Customer_Guide_*.md` + matching `.pdf` (customer-facing writeup), plus
  `Secondary_Findings_Product_Bugs.md` for bugs found along the way that aren't the primary
  subject of a ticket.

## Deploy / validate / destroy workflow

Run from `blueprints/`:

```bash
ROOT=02-at-scale make deploy              # terraform init + plan + apply (asks for confirmation unless CI=true)
ROOT=02-at-scale make validate            # post-deploy health probes
DESTROY_WL_ONLY=false ROOT=02-at-scale make destroy   # full teardown: CBCI workloads -> EKS -> rest of the stack
DESTROY_WL_ONLY=true  ROOT=02-at-scale make destroy   # workloads-only: removes CBCI/addons, keeps EKS/VPC running
```

- `make destroy`'s embedded confirmation prompt reads from stdin (`ask-confirmation` in
  `helpers.sh`) — pass `CI=true` to skip it when confirmation has already happened in chat.
- `tfChecks` requires `<ROOT>/.auto.tfvars` to exist, and for `02-at-scale` also requires
  `<ROOT>/k8s/secrets-values.yml`.
- AWS auth: SSO profile `324005994172_infra-admin` (sso-session `cloudbees`, account
  `324005994172`, role `infra-admin`), region `us-west-2`. Refresh with
  `aws sso login --sso-session cloudbees --no-browser` if `aws sts get-caller-identity`
  fails — the cached SSO session token is long-lived, but per-role credentials expire
  faster and need periodic re-derivation.
- **`hosted_zone` (`cikeysight.swen.aws.ps.beescloud.com`) is a Terraform *data source*,
  not a managed resource** (`data "aws_route53_zone" "this"` in `main.tf`) — it pre-exists
  outside this stack and `terraform destroy` will never remove it. If it needs to go, delete
  it manually via the AWS CLI *after* `terraform destroy` completes (it manages a wildcard
  record + ACM validation record inside the zone; `external-dns` running in-cluster may also
  leave orphaned ingress records — e.g. Grafana — that block zone deletion until removed).
- This AWS account is **shared** across many teams' hosted zones/environments (visible via
  `aws route53 list-hosted-zones-by-name`) — always scope destructive AWS CLI actions to a
  specific resource ID, never a bulk/wildcard operation.
- Full `terraform destroy` of `02-at-scale` takes ~20-25 minutes (EKS node group deletion
  and Helm release cleanup dominate). Run it backgrounded and poll the log rather than
  blocking.

## CasC bundle conventions

- Bump the bundle's `version:` in `bundle.yaml` on every content change — CasC uses it to
  detect updates.
- **CasC hot-reload polling is unreliable in this environment.** The most reliable way to
  force a bundle change to actually take effect is a full pod/PVC recreate on the affected
  controller (or at minimum a full pod restart). Don't assume a change is live just because
  it was pushed and enough time passed — verify against the live pod's persisted state.
- The Operations Center and each managed controller have **separate, independent RBAC
  configs**. A grant in `casc/oc/rbac.yaml` does **not** propagate to managed controllers —
  the equivalent grant must also exist in `casc/mc/mc-parent/rbac.yaml` (or wherever that
  controller's CasC bundle chain resolves RBAC from).
- As of the Test 3 changes, the OC's `securityRealm` is `oic-auth` (Microsoft Entra ID
  OIDC), not LDAP — there is no local `admin`/password account anymore. Basic-auth-based
  diagnostics (e.g. hitting `/scriptText`) will 401; use `kubectl exec` filesystem
  inspection instead when live script-console access isn't available.

## Known product-side gotchas (see `Secondary_Findings_Product_Bugs.md` for full detail)

- `CasCAutomaticControllerProvisioningTask` has a boot-time race that can throw a NPE
  (`PROVISIONING_ERROR`) for a controller shortly after OC startup; a full OC pod restart
  usually (not always) lets a retry succeed.
- A managed controller's persisted OC-side `config.xml` (under
  `$JENKINS_HOME/jobs/<controller>/config.xml` on the OC pod) has an `<approved>` flag;
  when `false`, the provisioning task silently skips that controller entirely (zero log
  output), a distinct and more silent failure mode than the NPE race above. Flipping it to
  `true` via `sed -i` + restart is a plausible remediation but was not confirmed sufficient
  on its own in the one case tested — treat as a partial finding, not a proven fix.

## Testing methodology

Every ticket investigation in `../tickets/` follows the same pattern: don't accept a
support/vendor claim ("not supported", "platform limitation", etc.) without trying to
actually reproduce it against a real, live-deployed instance first. Capture real commands
and real output as evidence — not just a narrative conclusion — in the `Test*_Results_*.md`
file, then distill that into a customer-facing `Customer_Guide_*.md`.
