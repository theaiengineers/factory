# Setup guide: stage 1

Stage 1 puts two stations in front of every change: the project's own tests, then an independent review. Nothing merges until both pass. This is the stage the factory supports today.

This page is written for an agent setting it up with the repository's owner. A person can follow it too. Run [the readiness check](READINESS.md) first; this guide uses its report.

## Rules for whoever runs it

1. **The owner decides; you propose.** Three decisions are theirs and are marked **Owner decides** below: the rules, who reviews, and making the checks required. Do not go past one of those without their answer.
2. **Everything goes in by pull request.** No direct pushes, including the files this guide adds.
3. **Do not weaken anything already there.** If the repository has branch rules or a review habit, add to them. Never replace or remove one.
4. **Never record a review that did not happen.** Not to test the gate, not to unblock yourself. The test in step 9 shows how to see the gate work without one.
5. **Say what you did not do.** If a step was skipped or could not be checked, the hand-over in step 10 says so.

## What you will add

| File in the repository | What it is |
|---|---|
| `factory.yml` | The rules: which checks must pass, which changes need a review, which paths are risky and why |
| `docs/REVIEW_STANDARD.md` | What a reviewer checks and how the result is recorded |
| `.github/workflows/factory-gate.yml` | A few lines that call the factory's gate at one fixed version |
| A paragraph in the notes agents read (`AGENTS.md`, `CLAUDE.md` or the contributor guide) | How work is done here now |

And one setting: the test checks and `independent-review` become required to merge.

## Step 1. Check readiness

Use the readiness report. If its verdict is not stage 1, stop and give the owner the gaps. If it has an "unknown" in section A, get it answered first.

## Step 2. Pick the factory version

```
git ls-remote --tags --sort=-v:refname https://github.com/theaiengineers/factory | head -2
```

Take the 40-character id on the line ending `^{}` for the newest tag. That commit id is the version. Write it down with the tag name; it is used in steps 3 and 6.

Use v0.2.0 or later. That is the first version with the starter files and the preview this guide uses; with an earlier one, steps 3 and 4 fail.

Get a copy of the factory at that version to work from, in a new directory outside the repository you are setting up:

```
FACTORY=$(mktemp -d)
git clone https://github.com/theaiengineers/factory "$FACTORY" && git -C "$FACTORY" checkout COMMIT
python3 -m venv "$FACTORY/.venv"
"$FACTORY/.venv/bin/pip" install --require-hashes --only-binary :all: -r "$FACTORY/requirements.txt"
```

The later steps call this directory `$FACTORY`.

## Step 3. Write the rules

Copy `starter/factory.yml` from the factory to the root of the repository as `factory.yml`, and fill it in from the readiness report's "For setup" section. [The factory file spec](../spec/factory-file.md) says what each key means.

**Owner decides** two lists. Put your proposal in front of them with the reason for each entry.

**The risky areas.** For each: a name, the paths, and a `why`. The `why` is shown to the reviewer on every high-risk change, so write what goes wrong for a real person, not what the code is ("moves money; a mistake charges real customers", not "billing module"). Fewer, sharper areas are better than many. If everything is high risk, nothing is.

**The paths a review may skip.** A change that touches only these needs no review at all. Start with prose (`"**/*.md"`) and stop there unless the owner asks for more. Three things to tell them before they add any:

- A skipped path is skipped even inside a risky area.
- Skipping tests means a change can delete a test, or loosen what it asserts, with nobody looking.
- Generated files are safe to skip only if something else checks they match their source.

Two files are always high risk whatever the lists say: `factory.yml` itself and the review standard it names.

When the file is filled in, `grep -n CHANGE factory.yml` should find nothing. The gate accepts a file with a placeholder left in; the rule simply never matches.

Set `require` under the `testing` station to the test check names from the readiness report. The gate does not enforce that list yet. It is the record of what step 8 makes required, and it is where a later factory version will read it from.

## Step 4. Try the rules on changes that have already merged

Before anyone depends on the rules, see what they would have said about recent work. From the root of the repository:

```
REPO=$PWD
gh pr list --state merged --limit 30 --json number,title,files |
  (cd "$FACTORY" && .venv/bin/python -m factory.preview --rules "$REPO/factory.yml")
```

It prints one line for each change with its level, then the totals. It calls nothing and changes nothing. If it stops with a one-sentence error, the file has a mistake; fix it and run again.

Read the output with the owner:

- A change they think of as risky that shows `code`: a risk area is missing, or its paths are wrong.
- A change they think of as routine that shows `high`: an area is drawn too wide.
- A change that shows `none` and touched something that runs: a skip path is too generous.
- A risk area listed as touched by no change: either a quiet area, or a path that does not match how the repository spells it. Check the path exists.

Change the rules and run it again until the owner is content. Its limits: it sees at most the first 100 files of a change (it marks a change listed with that many) and only the new name of a renamed file, so the real gate can be stricter than this preview on very large changes and on moves. If the file has no `independent-review` station, the preview says so and shows every change as needing no review, because the gate then checks nothing.

## Step 5. Add the review standard

Copy `docs/REVIEW_STANDARD.md` from the factory to the same path in the repository, or point `standard:` in `factory.yml` at a standard the team already has. If the team writes its own, it must keep the record section word for word: the gate reads that marker and nothing else.

## Step 6. Add the workflow

Copy `starter/factory-gate.yml` from the factory to `.github/workflows/factory-gate.yml` and replace `COMMIT`, in both places, with the id from step 2.

Check before you commit:

- Both places carry the same full commit id. A tag name or a branch name there means the code that judges every change can be swapped without a review.
- The `permissions` block is unchanged. The job needs to write one status and read pull requests, and nothing more.
- The file is valid YAML: `"$FACTORY/.venv/bin/python" -c 'import sys, yaml; yaml.safe_load(open(sys.argv[1]))' .github/workflows/factory-gate.yml` prints nothing when it is.

If the repository already enforces a review rule of its own and the owner wants to compare before switching, uncomment `context: independent-review-trial`. The gate then reports under that name beside the existing rule, and step 8 leaves it out of the required checks.

## Step 7. Arrange the reviewer, and tell the agents that work here

**Owner decides** who reviews.

The gate needs a comment on the pull request, from an account that can write to the repository, whose first line is the marker for the exact head commit. How the review comes to be done is up to the team. Two arrangements work today:

- **A person** reads the change against the standard and posts the record.
- **A fresh agent**, started for this one review and given only the pull request and the standard. It must not be the session that wrote the change, and it must not be shown the author's reasoning: a reviewer handed the author's conclusion tends to confirm it. A brief that works:

  > Review pull request NUMBER in OWNER/REPO against docs/REVIEW_STANDARD.md in that repository. You did not write this change and you have been told nothing about it on purpose. Read the diff, follow the changed code through its callers, and run the tests that cover it. Post your result as a new comment in the form the standard gives, for the head commit as it is when you finish. If you find something blocking, record findings; do not fix it yourself.

A reviewer that posts through a GitHub App does not count, and neither does the workflow's own token. If author and reviewer share one login, the gate cannot tell them apart; the standard says so, and the owner should know it.

Then add a short section to the file the repository's agents read at the start of their work. Without it, an authoring agent does not know the line exists. Adapt this:

> **How changes are made here.** Every change goes in by pull request. Run the tests before you open one: `THE TEST COMMAND`. Do not review your own change and do not post a review record for it. When the checks are green, ask for an independent review as `docs/REVIEW_STANDARD.md` describes. A new commit after a review needs a new review. Changes to `factory.yml` and the review standard are always high risk, and so is anything in the risk areas `factory.yml` lists. If a check blocks you, say so; do not look for a way round it.

## Step 8. Merge, then make the checks required

Open one pull request with the files from steps 3, 5, 6 and 7. The gate will not report on it: the workflow only starts once it is on the default branch. Have it reviewed in the ordinary way and merge it.

Then do step 9 once, with nothing required yet, to see the gate report.

**Owner decides** the last part, and an administrator makes it. In the rules for the default branch:

- require a pull request before merging;
- require these status checks: each test check from the readiness report, and `independent-review`;
- do not let administrators bypass the rules. An administrator's merge that skips the gate is the one nobody will notice.

Read the rules that are there before changing anything, and add to them. If you make the change through the API, do not send a request that replaces the whole protection with a new one: it silently drops every rule you did not repeat. Afterwards read the rules back and show the owner the result.

Once `independent-review` is required, a pull request on which the gate has not reported cannot merge. That is intended. It also means that if the workflow is ever removed or cannot start, merging stops until it is fixed or the requirement is lifted (see "Turning it off").

## Step 9. See it work

Open three small pull requests and watch the `independent-review` status on each. Close the ones that were only for testing.

| Change | What the status should say |
|---|---|
| Only a file the review skips, such as a line of prose | Passed: "No code changed: no independent review needed" |
| A line of real code outside every risk area | Not passed: "No independent review recorded for" the commit. The summary on the run lists the code that changed |
| A file inside a risk area | Not passed, naming the area. The summary gives the area, the file and the rule |

On one that is waiting, have the reviewer from step 7 do a real review and post the record. The status should turn to passed once the workflow has run again, which the comment starts. Then push another commit to it: the status should go back to not passed, saying the review is for the earlier commit.

If the status never appears, look at the workflow's runs. The usual causes: the workflow is not on the default branch yet; outside workflows are not allowed (readiness A1); `COMMIT` was not replaced. If it says `error`, the description says what could not be read; it is usually `factory.yml` failing to parse on the default branch.

## Step 10. Hand over

Tell the owner, in plain words:

- what is now required to merge, and which factory version is pinned;
- the rules they agreed, and where to change them (`factory.yml`, by pull request, always reviewed);
- who reviews;
- anything from the readiness report still open, with the worst test gap first;
- what was skipped or could not be checked.

## Keeping it running

**Changing the rules.** Edit `factory.yml` by pull request. The gate reads the rules from the default branch, so a change to the rules is judged by the rules as they were, and takes effect when it merges. Run step 4 again before proposing it.

**Taking a new factory version.** Nothing arrives by itself. To move, replace the commit id in both places in `factory-gate.yml`, by pull request. Read what changed between the two versions first. To go back, restore the old id.

**Turning it off.** Remove `independent-review` from the required checks, then remove the workflow. Do it in that order, or merging is blocked in between. Both are the owner's decision.

## What stage 1 does not do

- It does not do the review. It checks that one was recorded for the exact commit.
- It does not check that the tests passed. The required checks in step 8 do that.
- It does not check that the reviewer is a different account from the author.
- It does nothing after a change merges: no trial run, no release decision, no release, no watch, no alerts. Those are stages 2 and 3, and their factory parts are not published yet.
- It works on GitHub only.
