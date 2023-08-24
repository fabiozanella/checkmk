#!/usr/bin/env python3
# Copyright (C) 2023 Checkmk GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.

import argparse
import datetime
import difflib
import logging
import re
import textwrap
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from typing import NamedTuple

from git.exc import (  # type: ignore[attr-defined] # BadName is defined in gitdb, but does not provide type hints
    BadName,
    GitCommandError,
)
from git.objects.blob import Blob
from git.objects.commit import Commit
from git.repo import Repo
from jinja2 import Environment, PackageLoader, select_autoescape, StrictUndefined, Template

from cmk.utils.mail import MailString, send_mail_sendmail, set_mail_headers
from cmk.utils.version import Version

from .. import load_werk
from ..werk import Class, Level, WerkTranslator

logger = logging.getLogger(__name__)


class Args(NamedTuple):
    repo_path: Path
    branch: str
    ref: str
    mail: str

    assume_no_notes_but: str

    do_send_mail: bool
    do_fetch_git_notes: bool
    do_push_git_notes: bool
    do_add_notes: bool

    @classmethod
    def new(cls, args: argparse.Namespace) -> "Args":
        return Args(
            repo_path=args.repo_path.absolute(),
            branch=args.branch,
            ref=args.ref,
            mail=args.mail,
            assume_no_notes_but=args.assume_no_notes_but,
            do_send_mail=args.do_send_mail,
            do_fetch_git_notes=args.do_fetch_git_notes,
            do_push_git_notes=args.do_push_git_notes,
            do_add_notes=args.do_add_notes,
        )


class File(NamedTuple):
    name: str
    content: str

    @classmethod
    def new(cls, path: str, blob: Blob) -> "File":
        return File(
            name=Path(path).name,
            content=blob.data_stream.read().decode("utf-8"),
        )


class WerkAdded(NamedTuple):
    file: File

    @property
    def action(self) -> str:
        return "created"


class WerkRemoved(NamedTuple):
    file: File

    @property
    def action(self) -> str:
        return "deleted"


class WerkModified(NamedTuple):
    file: File
    diff: str

    @property
    def action(self) -> str:
        return "adapted"


class WerkCommit(NamedTuple):
    changes: list[WerkAdded | WerkRemoved | WerkModified]
    commit: Commit


def _is_werks_path(path: str | None) -> bool:
    if path is None:
        return False
    return re.match(r".werks/\d+", path) is not None


def get_change(commit: Commit) -> WerkCommit | None:
    def _collect():
        for diff in commit.parents[0].diff(commit):
            a_is_werk = _is_werks_path(diff.a_path)
            b_is_werk = _is_werks_path(diff.b_path)
            if not a_is_werk and not b_is_werk:
                continue

            if diff.renamed_file:
                if a_is_werk:
                    yield WerkRemoved(File.new(diff.a_path, diff.a_blob))
                if b_is_werk:
                    yield WerkAdded(File.new(diff.b_path, diff.b_blob))
            elif diff.deleted_file:
                if a_is_werk:
                    yield WerkRemoved(File.new(diff.a_path, diff.a_blob))
            elif diff.new_file:
                if b_is_werk:
                    yield WerkAdded(File.new(diff.b_path, diff.b_blob))
            elif diff.copied_file:
                if b_is_werk:
                    yield WerkAdded(File.new(diff.b_path, diff.b_blob))
            else:
                assert diff.b_path == diff.a_path
                werk_diff = "\n".join(
                    difflib.ndiff(
                        diff.a_blob.data_stream.read().decode("utf-8").split("\n"),
                        diff.b_blob.data_stream.read().decode("utf-8").split("\n"),
                    )
                )
                yield WerkModified(File.new(diff.b_path, diff.b_blob), werk_diff)

    werk_changes = list(_collect())
    if werk_changes:
        return WerkCommit(werk_changes, commit)
    return None


def _is_same_commit(repo: Repo, commit: Commit, commit_hash: str) -> bool:
    return commit == repo.commit(commit_hash)


def has_note(repo: Repo, commit: Commit, args: Args) -> bool:
    # normally we check if the git commit has a note, but for testing purposes
    # we also want to ignore all notes, and specify the first commit with note
    # via git hash:
    if args.assume_no_notes_but:
        return _is_same_commit(repo, commit, args.assume_no_notes_but)

    try:
        repo.git.notes(f"--ref={args.ref}", "show", commit)
    except GitCommandError:
        return False
    return True


def add_note(repo: Repo, commit: Commit, args: Args) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    note = f"Mail sent: {now.isoformat()}"
    repo.git.notes(f"--ref={args.ref}", "add", "-m", note, commit.hexsha)
    logger.info("added note to commit %s: %s", commit.hexsha, note)


def get_werk_commits(repo: Repo, branch_name: str, args: Args) -> Sequence[WerkCommit]:
    # first we have to check if there are notes at all, otherwise we might send
    # mails for all existing commits if we forgot to fetch the notes.

    if not repo.git.notes(f"--ref={args.ref}", "list"):
        raise RuntimeError(
            f"Could not find any notes with ref={args.ref}. "
            f"You may use --do-fetch-git-notes to fetch them from remote."
        )

    logger.info("notes sanity check passed")

    if args.assume_no_notes_but:
        # make sure that assume_no_notes_but passes some sanity checks, otherwise we
        # might have a typo in assume_no_notes_but and send mails for all commits.
        try:
            repo.commit(args.assume_no_notes_but)
        except (KeyError, BadName) as e:
            raise RuntimeError(f"could not find commit {args.assume_no_notes_but}") from e

        for commit in repo.iter_commits(branch_name):
            # there are faster ways to do this, but we want to have the same logic as in the main loop
            # TODO: think about adding a sanity check about the number of commits you have to step though?
            if _is_same_commit(repo, commit, args.assume_no_notes_but):
                logger.info("assume_no_notes_but sanity check passed")
                break
        else:
            raise RuntimeError(f"{args.assume_no_notes_but} is not part of {branch_name}")

    werk_changes = []

    for commit in repo.iter_commits(branch_name):
        logger.debug("checking commit %s", commit)
        if has_note(repo, commit, args):
            # found a note -> we already sent mails for this change, so we assume
            # all changes before this change are covered, we can stop searching for more.
            break

        # no note for this commit, so let's see if werks have been changed
        change = get_change(commit)
        if change:
            logger.debug("commit %s contains werk changes", commit)
            werk_changes.append(change)
    else:
        raise RuntimeError(
            "Could not find any commit that has git notes attached to it.\n"
            f"Checked notes ref {args.ref} on branch {branch_name}. "
            f"Manually add a git notes, otherwise this script would send mails for every werk change made on branch {branch_name}."
        )

    werk_changes.reverse()
    return werk_changes


def git_notes_fetch(repo: Repo, args: Args) -> None:
    repo.git.fetch("origin", f"refs/notes/{args.ref}:refs/notes/{args.ref}")


def git_notes_push(repo: Repo, args: Args) -> None:
    repo.git.push("origin", f"refs/notes/{args.ref}")


def send_mail(
    change: WerkModified | WerkAdded | WerkRemoved,
    template: Template,
    translator: WerkTranslator,
    args: Args,
) -> None:
    werk = load_werk(change.file.name, change.file.content)

    base_version = str(Version.from_str(werk.version).base)

    if werk.class_ == Class.SECURITY:
        mail_address = "checkmk-werks-sec@lists.checkmk.com"
    else:
        if werk.level == Level.LEVEL_1:
            mail_address = "checkmk-werks-lvl1@lists.checkmk.com"
        elif werk.level == Level.LEVEL_2:
            mail_address = "checkmk-werks-lvl2@lists.checkmk.com"
        elif werk.level == Level.LEVEL_3:
            mail_address = "checkmk-werks-lvl3@lists.checkmk.com"
        else:
            raise NotImplementedError()

    if args.mail:
        mail_address = args.mail.replace("@", "+" + mail_address.replace("@", "%") + "@")

    subject = f"[{base_version}] Checkmk Werk {werk.id} {change.action}: {werk.title}"
    message = template.render(
        werk=werk,
        werk_plaintext=change.file.content,
        component=translator.component_of(werk),
        class_=translator.class_of(werk),
        change=change,
        werk_modified=isinstance(change, WerkModified),
        werk_removed=isinstance(change, WerkRemoved),
    )

    mail = EmailMessage()
    mail.set_content(message, cte="quoted-printable")
    set_mail_headers(
        MailString(mail_address),
        MailString(subject),
        MailString("noreply@checkmk.com"),  # keep this in sync with the mailing lists settings
        MailString(""),
        mail,
    )

    if args.do_send_mail:
        send_mail_sendmail(mail, MailString(mail_address), MailString(""))
    else:
        print(textwrap.indent(mail.as_string(), "DRY RUN: ", lambda line: True))


def main(argparse_args: argparse.Namespace) -> None:
    args = Args.new(argparse_args)

    logging.basicConfig(level=logging.INFO)
    logger.info("repo_path=%s", args.repo_path)
    logger.info("branch=%s", args.branch)
    logger.info("ref=%s", args.ref)

    env = Environment(
        loader=PackageLoader("cmk.utils.werks.mail", "templates"),
        autoescape=select_autoescape(),
        undefined=StrictUndefined,
    )
    template = env.get_template("mail.txt.jinja2")

    translator = WerkTranslator()

    repo = Repo(args.repo_path)

    if args.do_fetch_git_notes:
        git_notes_fetch(repo, args)

    werk_commits = get_werk_commits(repo, args.branch, args)

    for werk_commit in werk_commits:
        # we add the note first, so maybe we will lose a werk mail
        # but this way we make sure we fail if the mail was already
        # sent out, as we can not add a second note (without -f)
        if args.do_add_notes:
            add_note(repo, werk_commit.commit, args)
        else:
            print(f"DRY RUN: add note to commit {werk_commit.commit}")

        for change in werk_commit.changes:
            logger.info(
                "Detected werk change in commit %s: %s %s",
                werk_commit.commit,
                change.file.name,
                change.__class__.__name__,
            )
            send_mail(change, template, translator, args)

        if args.do_push_git_notes:
            git_notes_push(repo, args)
