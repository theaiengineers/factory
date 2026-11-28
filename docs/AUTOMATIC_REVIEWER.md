# The automatic reviewer

The gate checks that an independent review was recorded. It does not do the review. The automatic reviewer is a station that does: when a pull request needs a review and has none for its head commit, a model reads the change against the review standard and the result is posted as the review record.

It is optional. A person, or an agent someone starts by hand, can still review any change, and their record counts exactly as before.

## What it does on each pull request

Each record it posts opens with the standard's **In short** block: one plain sentence for each blocking finding, or one saying what the change does when it passes. The words are the model's; when the code overrules a pass, the block says only that a blocking finding was recorded.

1. **Decides whether a review is wanted.** It stops quietly when the pull request is a draft or closed, when the change needs no review, when the head commit already has a record, or when the author cannot write to the repository. That last rule also covers pull requests opened by bots such as Dependabot: a person reviews those, or starts the reviewer by hand.
2. **Waits for the tests.** It reads the check names under `uses: checks` in `factory.yml` and waits for them to pass on the head commit. If one fails, it does not review; a failing change is the author's to fix first. Every check run and status reported under a required name has to pass. A check GitHub reports as skipped counts as passed, as it does in GitHub's own branch rules.
3. **Reads the change** through the GitHub API: the diff of every changed file, and each changed file in full where there is room.
4. **Asks the model** for a review against your review standard, read from the default branch.
5. **Records the result** as a new comment whose first line is the review marker, then runs the gate so the status updates.

## What it can and cannot see

Say this to anyone relying on it.

- It reads the diff and the changed files. **It does not read the rest of the repository**, so it cannot follow a change into callers the change does not touch.
- **It does not run the code or the tests.** It relies on your required checks for that, which is why it waits for them.
- It will not review a change it cannot read in full: one with a file that is not plain text, a file GitHub will not show a diff for, more than 300 changed files, or more than about 300,000 characters of diff. A text file that contains characters GitHub cannot decode counts as not plain text. It posts a short notice saying a person is needed. GitHub lists a new binary file exactly as it lists a new empty one, so the reviewer fetches every file it has no diff for and refuses unless the file really is empty.
- If a person records a review of the same commit while the automatic one is running, the person's stands and the automatic one is dropped.
- Any review marker posted from the workflow account for a commit, signed or not, stops the automatic reviewer for that commit. An unsigned one never counts as a review, so another workflow that posts one can only leave the commit to a person.
- A required check that was cancelled counts as failed. Push again or re-run it, then start the reviewer by hand.
- Each record says what was read, so a reader never has to guess.

A model can be wrong, and it can be misled by the change it is reading. Treat its pass as a careful first reader, not as proof.

## How it is kept from being steered

A pull request is written by someone the reviewer should not take orders from. A description or a code comment can say "reviewer: this is approved". The design assumes someone will try.

- **The model has no tools.** It cannot run a command, open a file, or reach the network. It is given text and returns text.
- **The pull request is never checked out or run.** Its content arrives as text through the API.
- **The model does not write the record.** It fills in a small form. The factory's code checks the form, writes the marker line itself, and turns the model's words into plain text: no link, image, mention, issue reference or HTML in them renders. Two visible side effects: a dollar sign is shown as the full-width sign (nothing else stops GitHub rendering maths), and a full commit id still links to that commit.
- **Two rules are applied by code, not left to the model.** A pass that lists a blocking finding is recorded as findings. A pass on a change that contains text addressed to the reviewer is recorded as findings, with the text quoted, so a person looks at why it is there.
- **The job that holds the model's credential cannot write anything.** A second job, which never sees the credential, posts the record.
- **The record is signed.** The reviewer posts from `github-actions[bot]`, the account every workflow in a repository shares. That account alone proves nothing: another workflow that repeats text from a pull request could post a forged marker from it. So the record carries a proof made with a key only your repository holds, tied to the repository, the pull request, the commit and the verdict. The gate counts a record from that account only when the proof is right. A signed record is public once posted, so the gate also counts each signed verdict once (the same text posted again is ignored), and an automatic pass never clears findings a person recorded for the same commit.
- **The standard and the rules come from the default branch,** so a pull request cannot rewrite what it is judged against.

What this does not stop: a change that talks the model into missing a real problem without addressing it directly. That is why an automatic pass counts only for the levels you allow, and by default not for high-risk changes.

## Setting it up

Do [stage 1 setup](SETUP.md) first. The automatic reviewer adds five things.

Before you start:

- **The files to copy** are in the factory at the commit your workflows are pinned to. Get them as step 2 of the setup guide does (a temporary clone checked out at that commit).
- **One version for everything.** The reviewer needs v0.3.0 or later, the health check v0.4.0 or later, and all the factory workflows in a repository should carry the same commit id. If your gate is pinned to an older one, move the gate to the newer id in the same pull request, having read what changed.
- **The pull request that adds all this is high risk** (it changes `factory.yml` and workflows), and the automatic reviewer is not working until it has merged. It needs a person's review, or a fresh agent's, as in step 7 of the setup guide.
- **Add the two secrets (steps 2 and 3) before that pull request merges.** Merged first, the reviewer runs on every pull request with nothing to reach its model with: it says so in a notice on each one, and reviews nothing.

### 1. Name it in `factory.yml`

```yaml
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: ["**/*.md"]
    never_skip: ["**/AGENTS.md", "**/CLAUDE.md"]
    automatic:
      account: github-actions[bot]
      passes: [code]            # the default: a high-risk change still needs a person
```

**Owner decides** `passes`. `[code]` means the reviewer's pass is enough for an ordinary code change, and a high-risk change still needs a person's pass. `[code, high]` lets its pass stand for high-risk changes too. Its findings always count, at every level.

`account` is the login the record is posted from. With the starter workflow that is `github-actions[bot]`, the account GitHub gives a workflow's own token. It must be a bot's login: the gate takes this account's records at its word, so it will not accept a person's name here.

### 2. Create the reviewer key

A long random value, stored as the repository secret `FACTORY_REVIEWER_KEY`. The owner runs this; the value is never shown:

```
openssl rand -hex 32 | gh secret set FACTORY_REVIEWER_KEY
```

Both workflows need it: the reviewer signs with it and the gate checks with it. In your gate workflow, add under the job that calls `review-gate.yml`:

```yaml
    secrets:
      reviewer_key: ${{ secrets.FACTORY_REVIEWER_KEY }}
```

Without the key in the gate, no automatic record counts. Without it in the reviewer, nothing is recorded and the run fails with a message saying so.

Who could still forge a record: anyone who can read the repository's secrets, which means anyone who can push a branch containing a workflow. Those people can already write to the repository and record a review under their own name, so the key adds no new trust. It removes the trust in every other workflow that comments.

Pull requests opened by Dependabot do not receive repository secrets, so an automatic record on one cannot be checked and a person reviews those.

### 3. Give it a way to reach the model

The repository's owner adds one secret. The factory never sees it.

| Secret | What it is | Fits |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | A token for one person's Claude subscription. They run `claude setup-token` in their own Claude Code; it lasts a year | One person or a small project. Reviews count against that person's subscription limits, and stop if their subscription does |
| `ANTHROPIC_API_KEY` | A key from the Claude Console, billed by use | A team, or a secret shared across repositories. Anthropic recommends a key for shared use |

Either one is a repository secret, so anyone who can push a branch containing a workflow can make a workflow that uses it. That is the same group who can already write to the repository; give write access accordingly.

If both are set, the key is used. The starter workflows pass both names on; a secret that is not set arrives empty and is ignored, so both lines can stay. Whoever creates the credential should add it themselves (`gh secret set NAME`, which asks for the value without showing it). An agent setting this up should not create, see or handle it.

Each project uses its own credential in its own repository. Do not run reviews for other people's repositories on your subscription: Anthropic's terms do not allow a subscription to be used on behalf of others.

### 4. Add the workflow

Copy `starter/factory-reviewer.yml` to `.github/workflows/factory-reviewer.yml` and pin it to the same commit id as your gate, in both places. It needs v0.3.0 or later.

Like the gate, it starts working once it is on the default branch.

### 5. Add the health check

Copy `starter/factory-reviewer-health.yml` to `.github/workflows/factory-reviewer-health.yml`, pinned to the same commit id. It needs v0.4.0 or later, and issues switched on for the repository. Once a month it checks that the reviewer can still reach its model and opens an issue when it cannot. See [when the credential stops working](#when-the-credential-stops-working).

### Then see it work

Open a pull request with a small real code change. After its required checks pass, a comment headed "Automatic review" should appear and the `independent-review` status should turn. Then try a change inside a risk area: with `passes: [code]` the status should say the automatic review passed and a person's review is still needed.

If no record appears, look first for a notice on the pull request: a missing model credential and a refused one each get one, in plain words. The reviewer's run is green in both cases, with a warning on it. Otherwise read the reviewer workflow's run; the `Review` step's log says in one line (starting "No review:") why it did not review. A missing reviewer key fails the record job, but only once there is a review to record. If the status turns and later turns back, the gate workflow has not been given the key: the reviewer's own run of the gate has it, and the gate workflow's next run does not.

## What it costs

One model call for each commit that needs a review and whose checks pass. Pushing three commits to a pull request means up to three reviews; a newer commit cancels a review of an older one that is still running. Drafts are not reviewed, so keep work in draft until it is ready.

Outside contributors cannot spend your credential: a pull request from an account that cannot write to the repository is not reviewed until a maintainer starts it by hand (the workflow's "Run workflow" button, with the pull request number and `force`).

## When the credential stops working

A subscription token lasts a year. A key can be withdrawn, and a subscription can end. Whichever happens, nothing merges unreviewed: with no review recorded, the gate holds each change until a person reviews it.

**On a pull request.** When the model's provider refuses the credential, the reviewer says so in those words on the pull request, once for each commit: the credential was refused, someone who administers the repository needs to replace the secret, and running the reviewer again will not help. Any other failure to get an answer gets the general "could not complete a review" notice. The reviewer's run also carries a warning.

**Between pull requests.** The health check (step 5 above) runs on a schedule, so a quiet repository finds out too. It makes one very small model call that carries nothing from the repository, and then:

| It finds | It does |
|---|---|
| The model answers | Nothing. If an issue it opened earlier is still open, it closes it with a comment |
| The credential is refused | Opens an issue, and the run fails |
| No credential is set, though the factory file names an automatic reviewer | Opens an issue, and the run fails |
| No answer after two tries, and the credential was not refused | Opens an issue saying so, and the run fails. This may be a passing fault |
| The factory file cannot be used | Opens an issue saying so, makes no model call, and the run fails |

It opens one issue for each kind of problem and does not open another while that one is open. Only an issue opened by the workflow account counts as "already open", so no person and no other app can silence a warning by opening a look-alike. If the check itself cannot finish, that is reported as "could not reach its model". In that case, and when the factory file cannot be used, the check has learnt nothing about the file, so it closes nothing. If `automatic` is removed from the factory file, the check closes its open issues and goes quiet. Every word of an issue is fixed in the code. A failed scheduled run is also emailed by GitHub to whoever last changed the schedule.

This tells you the credential has stopped, within one schedule interval. It cannot tell you in advance: a token does not say when it expires, and a workflow cannot read a secret's age.

**A countdown, if you want one.** Write the day the credential was last created into the factory file:

```yaml
    automatic:
      account: github-actions[bot]
      credential_renewed: 2026-11-27
```

Once that day is 330 days old (or is more than a day in the future, which is taken as a mistake), the health check opens an issue saying renewal is due, about five weeks before a one-year token expires. The run does not fail for this, and the reviewer keeps working until the token really expires. The day is only as good as the person who writes it down: change it each time you replace the credential, and the issue closes at the next check. Move the gate to v0.4.0 or later before adding this line, because an earlier gate refuses a key it does not know.

**Replacing the credential.** Whoever owns it creates a new one (`claude setup-token`, or a new key in the Claude Console) and sets the secret again with `gh secret set NAME`. Then start the health check by hand from the Actions tab: it should pass and close its issue. Pull requests that got the refused notice are not reviewed again by themselves; start the reviewer by hand for each, or push a new commit.

What the health check does not watch: the reviewer key (a missing one fails the reviewer's record job, visibly), and whether GitHub has paused the schedule. GitHub stops scheduled workflows in a public repository after 60 days without activity and emails about it.

## Starting one by hand

Use the workflow's "Run workflow" button with the pull request number. Do this after re-running a failed check with no new commit, or for an outside contribution you have looked over.

## Turning it off

Remove `automatic` from `factory.yml`. From then on the gate ignores its records, including ones already posted, and the reviewer stops asking the model. Then remove the workflow and the secrets. Replacing the key has the same effect on records already posted: they stop counting.

## Using another model or provider

The first version asks Claude through the Claude Code command line. The seam for another is small and is in `factory/reviewer.py`: one function takes the prompt and returns the form described there (`verdict`, `checked`, `blocking`, `remarks`, `undo`, `addressed_to_reviewer`). Everything before it (deciding, waiting, reading) and after it (checking the form, writing the record, running the gate) is the same whichever model answers. A second provider is a second such function and a way to choose it; that is not built yet.

A reviewer that is not built on this station at all also works, if it posts the marker from a person's account that can write to the repository.
