import time
import math

import regex
from sqlalchemy.orm import *

from pynab import log
from pynab.db import db_session, engine, Binary, Part, Release, Group, Category, Blacklist
import pynab.categories
import pynab.nzbs
import pynab.rars
import pynab.nfos
import pynab.sfvs
import config


def names_from_nfos(release):
    """Attempt to grab a release name from its NFO."""
    nfo = pynab.nfos.get(release.nfo).decode('ascii', 'ignore')
    if nfo:
        return pynab.nfos.attempt_parse(nfo)
    else:
        return []


def names_from_files(release):
    """Attempt to grab a release name from filenames inside the release."""
    potential_names = []
    for file in release.files:
        name = pynab.rars.attempt_parse(file.name)
        if name:
            potential_names.append(name)
    return potential_names


def names_from_sfvs(release):
    """Attempt to grab a release name from supplied SFV (if it exists)."""
    sfv = pynab.sfvs.get(release.sfv).decode('ascii', 'ignore')
    if sfv:
        return pynab.sfvs.attempt_parse(sfv)
    else:
        return []


def discover_name(release):
    """Attempts to fix a release name by nfo, filelist or sfv."""
    potential_names = [release.search_name, ]

    if release.files:
        potential_names += names_from_files(release)

    if release.nfo:
        potential_names += names_from_nfos(release)

    if release.sfv:
        potential_names += names_from_sfvs(release)

    if len(potential_names) > 1:
        old_category = release.category_id
        calculated_old_category = pynab.categories.determine_category(release.search_name)

        for name in potential_names:
            new_category = pynab.categories.determine_category(name)

            # the release may already be categorised by the group it came from
            # so if we check the name and it doesn't fit a category, it's probably
            # a shitty name
            if (math.floor(calculated_old_category / 1000) * 1000) == pynab.categories.CAT_PARENT_MISC:
                # sometimes the group categorisation is better than name-based
                # so check if they're in the same parent and that parent isn't misc
                if (math.floor(new_category / 1000) * 1000) == pynab.categories.CAT_PARENT_MISC:
                    # ignore this name, since it's apparently gibberish
                    continue
                else:
                    if (math.floor(new_category / 1000) * 1000) == (math.floor(old_category / 1000) * 1000) \
                            or (math.floor(old_category / 1000) * 1000) == pynab.categories.CAT_PARENT_MISC:
                        # if they're the same parent, use the new category
                        # or, if the old category was misc>other, fix it
                        search_name = name
                        category_id = new_category

                        log.info('release: [{}] - [{}] - rename: {} ({} -> {} -> {})'.format(
                            release.id,
                            release.search_name,
                            search_name,
                            old_category,
                            calculated_old_category,
                            category_id
                        ))

                        return search_name, category_id
                    else:
                        # if they're not the same parent and they're not misc, ignore
                        continue
            else:
                # the old name was apparently fine
                log.info('release: [{}] - [{}] - old name was fine'.format(
                    release.id,
                    release.search_name
                ))
                return False, calculated_old_category

    log.info('release: [{}] - [{}] - no good name candidates'.format(
        release.id,
        release.search_name
    ))
    return None, None


def clean_release_name(name):
    """Strip dirty characters out of release names. The API
    will match against clean names."""
    chars = ['#', '@', '$', '%', '^', '§', '¨', '©', 'Ö']
    for c in chars:
        name = name.replace(c, '')
    return name.replace('_', ' ').replace('.', ' ').replace('-', ' ')


def process():
    """Helper function to begin processing binaries. Checks
    for 100% completion and will create NZBs/releases for
    each complete release. Will also categorise releases,
    and delete old binaries."""

    # TODO: optimise query usage in this, it's using like 10-15 per release

    binary_count = 0
    added_count = 0

    start = time.time()

    with db_session() as db:
        binary_query = """
            SELECT
                binaries.id, binaries.name, binaries.posted
            FROM binaries
            INNER JOIN (
                SELECT
                    parts.id, parts.binary_id, parts.total_segments, count(*) as available_segments
                FROM parts
                    INNER JOIN segments ON parts.id = segments.part_id
                GROUP BY parts.id
                ORDER BY parts.posted DESC
                ) as parts
                ON binaries.id = parts.binary_id
            GROUP BY binaries.id
            HAVING count(*) >= binaries.total_parts AND (sum(parts.available_segments) / sum(parts.total_segments)) * 100 >= {}
        """.format(config.postprocess.get('min_completion', 100))

        # pre-cache blacklists and group them
        blacklists = db.query(Blacklist).filter(Blacklist.status==True).all()

        # for interest's sakes, memory usage:
        # 38,000 releases uses 8.9mb of memory here
        # no real need to batch it, since this will mostly be run with
        # < 1000 releases per run
        for completed_binary in engine.execute(binary_query).fetchall():
            # some optimisations here. we used to take the binary id and load it
            # then compare binary.name and .posted to any releases
            # in doing so, we loaded the binary into the session
            # this meant that when we deleted it, it didn't cascade
            # we had to submit many, many delete queries - one per segment/part
            # by including name/posted in the big query, we don't load that much data
            # but it lets us check for a release without another query, and means
            # that we cascade delete when we clear the binary

            # first we check if the release already exists
            r = db.query(Release).filter(Release.name == completed_binary[1]).filter(
                Release.posted == completed_binary[2]
            ).first()
            if r:
                # if it does, we have a duplicate - delete the binary
                db.query(Binary).filter(Binary.id == completed_binary[0]).delete()
            else:
                # otherwise, start loading all the binary details
                binary = db.query(Binary).options(
                    subqueryload('parts'),
                    subqueryload('parts.segments'),
                    Load(Part).load_only(Part.id, Part.subject, Part.segments),
                ).filter(Binary.id == completed_binary[0]).first()

                blacklisted = False
                for blacklist in blacklists:
                    if regex.search(blacklist.group_name, binary.group_name):
                        # we're operating on binaries, not releases
                        field = 'name' if blacklist.field == 'subject' else blacklist.field
                        if regex.search(blacklist.regex, getattr(binary, field)):
                            log.info('release: [{}] - removed (blacklisted: {})'.format(binary.name, blacklist.id))
                            db.delete(binary)
                            db.commit()
                            blacklisted = True
                            break

                if blacklisted:
                    continue

                binary_count += 1

                release = Release()
                release.name = binary.name
                release.posted = binary.posted
                release.posted_by = binary.posted_by
                release.regex_id = binary.regex_id
                release.grabs = 0
                release.size = binary.size()

                # check against minimum size for this group
                undersized = False
                for size, groups in config.postprocess.get('min_size', {}).items():
                    if binary.group_name in groups:
                        if release.size < size:
                            undersized = True
                            break

                if undersized:
                    log.info('release: [{}] - removed (smaller than minimum size for group)'.format(
                        binary.name
                    ))
                    db.delete(binary)
                    db.commit()
                    continue


                # check to make sure we have over the configured minimum files
                rars = []
                rar_count = 0
                zip_count = 0

                for part in binary.parts:
                    if regex.search(pynab.nzbs.rar_part_regex, part.subject):
                        rar_count += 1
                    if regex.search(pynab.nzbs.rar_regex, part.subject) and not regex.search(pynab.nzbs.metadata_regex,
                                                                                             part.subject):
                        rars.append(part)
                    if regex.search(pynab.nzbs.zip_regex, part.subject) and not regex.search(pynab.nzbs.metadata_regex,
                                                                                             part.subject):
                        zip_count += 1

                if rar_count + zip_count < config.postprocess.get('min_archives', 1):
                    log.info('release: [{}] - removed (less than minimum archives)'.format(
                        binary.name
                    ))
                    db.delete(binary)
                    db.commit()
                    continue

                # clean the name for searches
                release.search_name = clean_release_name(binary.name)

                # assign the release group
                release.group = db.query(Group).filter(Group.name == binary.group_name).one()

                # give the release a category
                release.category_id = pynab.categories.determine_category(binary.name, binary.group_name)
                category = db.query(Category).filter(Category.id == release.category_id).one()

                # create the nzb, store it in GridFS and link it here
                nzb = pynab.nzbs.create(release.search_name, category, binary)

                if nzb:
                    added_count += 1

                    log.debug('release: [{}]: added release ({} rars, {} rarparts)'.format(
                        release.search_name,
                        len(rars),
                        rar_count
                    ))

                    release.nzb = nzb

                    # save the release
                    db.add(release)

                    # delete processed binaries
                    db.delete(binary)

            db.commit()

    end = time.time()
    log.info('release: added {} out of {} binaries in {:.2f}s'.format(
        added_count,
        binary_count,
        end - start
    ))
