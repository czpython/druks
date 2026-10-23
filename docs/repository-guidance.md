---
title: "Steer Software Factory for a repository"
description: "The files and settings a repository uses to guide Software Factory builds, who reads each one, and when a change takes effect."
sidebarTitle: "Steer a repository's builds"
icon: "compass"
---

A repository can teach Software Factory how it works: which commands verify a change, what reviewers must check, and what the agents should read first. Each source of guidance reaches the agents at a different time. A change that misses that point waits for the next build.

| Guidance | Where it lives | Who reads it | When a change takes effect |
| --- | --- | --- | --- |
| Build policy | `.druks/software_factory/config.yml` on the default branch | The build | The next build. A build records the policy when it starts. See [repository configuration](/configuration#repository-configuration). |
| Repository profile | The repository's row in **Software Factory → Projects** | Every build step, for the verification commands and the recommended skills | The next build, after a new profile. A build records the profile when it starts. |
| Skills | Installed skill collections, see [skills](/configuration#skills) | Every agent call in a build that requests them | The next build, after a new profile. A build requests the recommended set that its profile names. |
| Review checklist | `.druks/review/checklist.md` in the checkout | The implementer, the plan reviewer, the code reviewer and the pull request review. The planner does not read it. | The next agent call on a branch that contains the file. A branch created before the commit does not have it. |
| Agent instructions | `AGENTS.md` and `CLAUDE.md` in the checkout | The agent harness, as it does in any checkout | The next agent call on a branch that contains the change |
| Ticket text and comments | The tracker | Every build step. The agent fetches the ticket first. | The next agent call |
| Prompt overrides | `.druks/software_factory/prompts/` on the default branch | The repository profiler only, for now. See [prompt overrides](#prompt-overrides). | The next profile |

## Change the profile or the skills

The profile holds the repository's verification commands and the skills that builds request. Druks profiles a repository when it joins a project. To profile it again, open **Software Factory → Projects**, select the repository and choose **Profile**.

After you install, sync, enable or disable skills, profile the repository again. The profiling agent chooses the recommended set from the enabled skills. A build that is already running keeps the profile it started with, and a retry keeps it too. Start a new build to use the new profile.

## Write a review checklist

`.druks/review/checklist.md` is the repository's standing rules for code and reviews. The plan reviewer, the code reviewer and the pull request review treat it as the last word where it and the built-in instructions disagree. The implementer reads it before writing code, because the reviewers hold the change to it. Keep it to rules that apply to every change, for example tests that must exist, patterns to avoid, or facts about the domain that reviewers keep missing.

Commit it to the default branch. Builds read it from their own branch, so a build that branched before the commit does not see it until its branch includes the commit.

## Check what an agent received

Each agent call writes its inputs under `<DRUKS_DATA_DIR>/artifacts/run-<run id>/<call id>/`. `prompt.md` is the rendered prompt, so you can confirm that the ticket, the checklist instructions and any override text are there. `manifest.json` records the skills that the call received in `skills_delivered`.

## Prompt overrides

A repository can replace a Software Factory prompt with its own file. Druks looks for the template in this order and uses the first that it finds:

1. `<repository>/.druks/software_factory/prompts/<path>`
2. `<owner>/.druks` repository, `software_factory/prompts/<path>`
3. The built-in template

`<path>` is the template path inside the app, for example `profile/repo_profiler.md`. Druks reads the files from the default branch on each agent call.

<Warning>
At the moment only the repository profiler applies overrides. Build steps such as `build/generate_plan.md` and `build/implement.md`, and the pull request review, always use the built-in templates. See [#968](https://github.com/czpython/druks/issues/968) and [#771](https://github.com/czpython/druks/issues/771). Use the review checklist, `AGENTS.md` and the ticket until that is fixed.
</Warning>

An override replaces the whole template. To keep the built-in prompt and add to it, include the built-in template first:

```jinja
{% include "software_factory/profile/repo_profiler.md" %}

## Repository notes

Add the guidance for this repository here.
```

Druks renders overrides in a sandboxed template environment, so an override cannot run code. It can still change what the agent is told, so anyone who can push to the default branch of the repository, or of `<owner>/.druks`, can change the instructions. An unknown template variable makes the agent call fail, so check an override before you push it.
