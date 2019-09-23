import logging

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.module_loading import import_string
import polib

from wagtail_localize.models import Language, ParentNotTranslatedError
from wagtail_localize.translation_memory.models import Segment

from .git import Repository
from .models import PontoonResourceSubmission, PontoonResource, PontoonSyncLog, PontoonSyncLogResource
from .pofile import generate_source_pofile, generate_language_pofile


logger = logging.getLogger(__name__)


def _try_update_resource_translation(resource, language):
    # Check if there is a submission ready to be translated
    translatable_submission = resource.find_translatable_submission(language)

    if translatable_submission:
        logger.info(f"Saving translated page for '{resource.page.title}' in {language.get_display_name()}")

        try:
            revision, created = translatable_submission.create_or_update_translated_page(language)
        except ParentNotTranslatedError:
            # These pages will be handled when the parent is created in the code below
            logger.info(f"Cannot save translated page for '{resource.page.title}' in {language.get_display_name()} yet as its parent must be translated first")

        if created:
            # Check if this page has any children that may be ready to translate
            child_page_resources = PontoonResource.objects.filter(
                page__in=revision.page.get_children()
            )

            for resource in child_page_resources:
                _try_update_resource_translation(resource, language)


@transaction.atomic
def _pull(repo):
    # Get the last commit ID that we either pulled or pushed
    last_log = PontoonSyncLog.objects.order_by('-time').exclude(commit_id='').first()
    last_commit_id = None
    if last_log is not None:
        last_commit_id = last_log.commit_id

    # Create a new log for this pull
    log = PontoonSyncLog.objects.create(
        action=PontoonSyncLog.ACTION_PULL,
        commit_id=repo.get_head_commit_id(),
    )

    current_commit_id = repo.get_head_commit_id()

    if last_commit_id == current_commit_id:
        logger.info("Pull: No changes since last sync")
        return

    for filename, old_content, new_content in repo.get_changed_files(last_commit_id, repo.get_head_commit_id()):
        logger.info(f"Pull: Importing changes in file '{filename}'")
        resource, language = PontoonResource.get_by_po_filename(filename)

        # Log that this resource was updated
        PontoonSyncLogResource.objects.create(
            log=log,
            resource=resource,
            language=language,
        )

        old_po = polib.pofile(old_content.decode('utf-8'))
        new_po = polib.pofile(new_content.decode('utf-8'))

        with transaction.atomic():
            for changed_entry in set(new_po) - set(old_po):
                try:
                    segment = Segment.objects.get(text=changed_entry.msgid)
                    translation, created = segment.translations.get_or_create(
                        language=language,
                        defaults={
                            'text': changed_entry.msgstr,
                            'updated_at': timezone.now(),
                        }
                    )

                    if not created:
                        # Update the translation only if the text has changed
                        if translation.text != changed_entry.msgstr:
                            translation.text = changed_entry.msgstr
                            translation.updated_at = timezone.now()
                            translation.save()

                            # TODO: Update previously translated pages that used this string?

                except Segment.objects.DoesNotExist:
                    logger.warning(f"Unrecognised segment '{changed_entry.msgid}'")

            # Check if the translated page is ready to be created/updated
            _try_update_resource_translation(resource, language)


@transaction.atomic
def _push(repo):
    reader = repo.reader()
    writer = repo.writer()
    writer.copy_unmanaged_files(reader)

    def update_po(filename, new_po_string):
        try:
            current_po_string = reader.read_file(filename).decode('utf-8')
            current_po = polib.pofile(current_po_string, wrapwidth=200)

            # Take metadata from existing PO file
            new_po = polib.pofile(new_po_string, wrapwidth=200)
            new_po.metadata = current_po.metadata
            new_po_string = str(new_po)

        except KeyError:
            pass

        writer.write_file(filename, new_po_string)

    languages = Language.objects.filter(is_active=True).exclude(id=Language.objects.default_id())

    paths = []
    pushed_submission_ids = []
    for submission in PontoonResourceSubmission.objects.filter(revision_id=F('resource__current_revision_id')).select_related('resource').order_by('resource__path'):
        source_po = generate_source_pofile(submission.resource)
        update_po(str(submission.resource.get_po_filename()), source_po)

        for language in languages:
            locale_po = generate_language_pofile(submission.resource, language)
            update_po(str(submission.resource.get_po_filename(language=language)), locale_po)

        paths.append((submission.resource.get_po_filename(), submission.resource.get_locale_po_filename_template()))

        pushed_submission_ids.append(submission.id)

    writer.write_config([language.as_rfc5646_language_tag() for language in languages], paths)

    # A queryset of submissions we've just written that haven't been pushed before
    pushed_submissions = PontoonResourceSubmission.objects.filter(id__in=pushed_submission_ids, push_log__isnull=True)

    if pushed_submissions.exists():
        # Create a new log for this push
        log = PontoonSyncLog.objects.create(
            action=PontoonSyncLog.ACTION_PUSH,
            commit_id='',
        )

        # Add an entry for each resource we just pushed
        for resource_id in pushed_submissions.values_list('resource_id', flat=True):
            PontoonSyncLogResource.objects.create(
                log=log,
                resource_id=resource_id,
            )

        pushed_submissions.update(pushed_at=timezone.now(), push_log=log)

        if writer.has_changes():
            logger.info("Push: Committing changes")
            writer.commit("Updates to source content")

            log.commit_id = repo.get_head_commit_id()
            log.save(update_fields=['commit_id'])
        else:
            logger.info("Push: Not committing anything as recent changes haven't affected any translatable content")

        repo.push()
    else:
        logger.info("Push: No changes since last sync")


class SyncManager:
    def __init__(self):
        self.logger = logger

    def sync(self):
        self.logger.info("Pulling repository")
        repo = Repository.open()
        repo.pull()

        _pull(repo)
        _push(repo)

        self.logger.info("Finished")

    def trigger(self):
        """
        Called when user presses the "Sync" button in the admin

        This should enqueue a background task to run the sync() function
        """
        self.sync()

    def is_queued(self):
        """
        Returns True if the background task is queued
        """
        return False

    def is_running(self):
        """
        Returns True if the background task is currently running
        """
        return False


def get_sync_manager():
    sync_manager_class_path = getattr(settings, 'WAGTAILLOCALIZE_PONTOON_SYNC_MANAGER_CLASS', 'wagtail_localize_pontoon.sync.SyncManager')
    sync_manager = import_string(sync_manager_class_path)
    return sync_manager()