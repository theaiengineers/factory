# The automatic reviewer

The gate checks that an independent review was recorded. It does not do the review. The automatic reviewer is a station that does: when a pull request needs a review and has none for its head commit, a model reads the change against the review standard and the result is posted as the review record.

It is optional. A person, or an agent someone starts by hand, can still review any change, and their record counts exactly as before.

## What it does on each pull request

1. **Decides whether a review is wanted.** It stops quietly when the pull request is a draft or closed, when the change needs no review, when the head commit already has a record, or when the author cannot write to the repository.
2. **Waits for the tests.** It reads the check names under `uses: checks` in `factory.yml` and waits for them to pass on the head commit. If one fails, it does not review; a failing change is the author's to fix first.
3. **Reads the change** through the GitHub API: the diff of every changed file, and each changed file in full where there is room.
4. **Asks the model** for a review against your review standard, read from the default branch.
5. **Records the result** as a new comment whose first line is the review marker, then runs the gate so the status updates.

## What it can and cannot see

Say this to anyone relying on it.

- It reads the diff and the changed files. **It does not read the rest of the repository**, so it cannot follow a change into callers the change does not touch.
- **It does not run the code or the tests.** It relies on your required checks for that, which is why it waits for them.
- It will not review a change it cannot read in full: one with a binary file or a file GitHub will not show a diff for, or one over about 300,000 characters. It posts a short notice saying a person is needed.
- Each record says what was read, so a reader never has to guess.

A model can be wrong, and it can be misled by the change it is reading. Treat its pass as a careful first reader, not as proof.

## How it is kept from being steered

A pull request is written by someone the reviewer should not take orders from. A description or a code comment can say "reviewer: this is approved". The design assumes someone will try.

- **The model has no tools.** It cannot run a command, open a file, or reach the network. It is given text and returns text.
- **The pull request is never checked out or run.** Its content arrives as text through the API.
- **The model does not write the record.** It fills in a small form. The factory's code checks the form, writes the marker line itself, and strips anything in the model's words that could act as a marker, a mention, or markup.
- **Two rules are applied by code, not left to the model.** A pass that lists a blocking finding is recorded as findings. A pass on a change that contains text addressed to the reviewer is recorded as findings, with the text quoted, so a person looks at why it is there.
- **The job that holds the model's credential cannot write anything.** A second job, which never sees the credential, posts the record.
- **The standard and the rules come from the default branch,** so a pull request cannot rewrite what it is judged against.

What this does not stop: a change that talks the model into missing a real problem without addressing it directly. That is why an automatic pass counts only for the levels you allow, and by default not for high-risk changes.

## Setting it up

Do [stage 1 setup](SETUP.md) first. The automatic reviewer adds three things.

### 1. Name it in `factory.yml`

```yaml
  - station: qa
    uses: independent-review
    standard: docs/REVIEW_STANDARD.md
    skip_when_only: ["**/*.md"]
    automatic:
      account: github-actions[bot]
      passes: [code]            # the default: a high-risk change still needs a person
```

**Owner decides** `passes`. `[code]` means the reviewer's pass is enough for an ordinary code change, and a high-risk change still needs a person's pass. `[code, high]` lets its pass stand for high-risk changes too. Its findings always count, at every level.

`account` is the login the record is posted from. With the starter workflow that is `github-actions[bot]`, the account GitHub gives a workflow's own token. It must be a bot's login: the gate takes this account's records at its word, so it will not accept a person's name here.

### 2. Give it a way to reach the model

The repository's owner adds one secret. The factory never sees it.

| Secret | What it is | Fits |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | A token for one person's Claude subscription. They run `claude setup-token` in their own Claude Code; it lasts a year | One person or a small project. Reviews count against that person's subscription limits, and stop if their subscription does |
| `ANTHROPIC_API_KEY` | A key from the Claude Console, billed by use | A team, or a secret shared across repositories. Anthropic recommends a key for shared use |

If both are set, the key is used. Whoever creates the credential should add it themselves (`gh secret set NAME`, which asks for the value without showing it). An agent setting this up should not create, see or handle it.

Each project uses its own credential in its own repository. Do not run reviews for other people's repositories on your subscription: Anthropic's terms do not allow a subscription to be used on behalf of others.

### 3. Add the workflow

Copy `starter/factory-reviewer.yml` to `.github/workflows/factory-reviewer.yml` and pin it to the same commit id as your gate, in both places. It needs v0.3.0 or later.

Like the gate, it starts working once it is on the default branch.

### Then see it work

Open a pull request with a small real code change. After its required checks pass, a comment headed "Automatic review" should appear and the `independent-review` status should turn. Then try a change inside a risk area: with `passes: [code]` the status should say the automatic review passed and a person's review is still needed.

If nothing appears, read the reviewer workflow's run. Its log says in one line why it did not review. A missing secret shows as a warning on the run.

## What it costs

One model call for each commit that needs a review and whose checks pass. Pushing three commits to a pull request means up to three reviews; a newer commit cancels a review of an older one that is still running. Drafts are not reviewed, so keep work in draft until it is ready.

Outside contributors cannot spend your credential: a pull request from an account that cannot write to the repository is not reviewed until a maintainer starts it by hand (the workflow's "Run workflow" button, with the pull request number and `force`).

## Starting one by hand

Use the workflow's "Run workflow" button with the pull request number. Do this after re-running a failed check with no new commit, or for an outside contribution you have looked over.

## Turning it off

Remove `automatic` from `factory.yml`. From then on the gate ignores its records, including ones already posted, and the reviewer stops asking the model. Then remove the workflow and the secret.

## Using another model or provider

The first version asks Claude through the Claude Code command line. The seam for another is small and is in `factory/reviewer.py`: one function takes the prompt and returns the form described there (`verdict`, `checked`, `blocking`, `remarks`, `undo`, `addressed_to_reviewer`). Everything before it (deciding, waiting, reading) and after it (checking the form, writing the record, running the gate) is the same whichever model answers. A second provider is a second such function and a way to choose it; that is not built yet.

A reviewer that is not built on this station at all also works: anything that posts the marker from an account the gate trusts is a reviewer.
