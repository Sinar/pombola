from collections import defaultdict
import csv
import datetime
import itertools
import json
import os
import re
import requests
import sys

from django.core.management.base import NoArgsCommand, CommandError
from django.template.defaultfilters import slugify

from django_date_extensions.fields import ApproximateDate

from settings import IEBC_API_ID, IEBC_API_SECRET
from optparse import make_option

from core.models import Place, PlaceKind, Person, ParliamentarySession, Position, PositionTitle, Organisation, OrganisationKind

from iebc_api import *

new_data_date = datetime.date(2013, 2, 8)
new_data_approximate_date = ApproximateDate(new_data_date.year,
                                            new_data_date.month,
                                            new_data_date.day)

data_directory = os.path.join(sys.path[0], 'kenyan-election-data')

# Calling these 'corrections' may not be quite right.  There are
# naming discrepancies between the documents published by the IEBC and
# the IEBC API in spellings of ward names in particular.  This maps
# the IEBC API version to what we have in the API (which for wards was
# derived from "Final Constituencies and Wards Description.pdf").

place_name_corrections = {}

with open(os.path.join(data_directory, 'wards-names-matched.csv')) as fp:
    reader = csv.reader(fp)
    for api_name, db_name in reader:
        if api_name and db_name:
            place_name_corrections[api_name] = db_name

party_name_corrections = {}

with open(os.path.join(data_directory, 'party-names-matched.csv')) as fp:
    reader = csv.reader(fp)
    for api_name, db_name in reader:
        if api_name and db_name:
            party_name_corrections[api_name] = db_name

same_people = {}

names_checked_csv_file = 'names-manually-checked.csv'
with open(os.path.join(data_directory, names_checked_csv_file)) as fp:
    reader = csv.DictReader(fp)
    for row in reader:
        classification = row['Same/Different']
        mz_id = int(row['Mz ID'], 10)
        candidate_code = row['API Candidate Code']
        key = (candidate_code, mz_id)
        if re.search('^Same', classification):
            same_people[key] = True
        elif re.search('^Different', classification):
            same_people[key] = False
        else:
            raise Exception, "Bad 'Same/Different' value in the line: %s" % (row,)






def get_person_from_names(first_names, surname):
    print "first_names:", first_names
    print "surname:", surname
    full_name = first_names + ' ' + surname
    first_and_last = re.sub(' .*', '', first_names) + ' ' + surname
    print "full_name:", full_name
    print "first_and_last:", first_and_last
    for field in 'legal_name', 'other_names':
        for version in (full_name, first_and_last):
            kwargs = {field + '__iexact': version}
            matches = Person.objects.filter(**kwargs)
            if len(matches) > 1:
                message = "Multiple Person matches for %s against %s" % (version, field)
                # print >> sys.stderr, message
                raise Exception, message
            elif len(matches) == 1:
                return matches[0]
    # Or look for an exact slug match:
    matches = Person.objects.filter(slug=slugify(full_name))
    if len(matches) == 1:
        return matches[0]
    matches = Person.objects.filter(slug=slugify(first_and_last))
    if len(matches) == 1:
        return matches[0]
    # Otherwise, look for the best hits using Levenshtein distance:
    for field in 'legal_name', 'other_names':
        for version in (full_name, first_and_last):
            closest_match = Person.objects.raw('SELECT *, levenshtein(legal_name, %s) AS difference FROM core_person ORDER BY difference LIMIT 1', [version])[0]
            if closest_match.difference <= 2:
                print "  good closest match to %s against %s was: %s (with score %d)" % (field, version, closest_match, closest_match.difference)
    return None

def get_matching_party(party_name, **options):
    party_name_to_use = party_name_corrections.get(party_name, party_name)
    # print "looking for '%s'" % (party_name_to_use,)
    matching_parties = Organisation.objects.filter(kind__slug='party',
                                                   name__iexact=party_name_to_use)
    if not matching_parties:
        matching_parties = Organisation.objects.filter(kind__slug='party',
                                                       name__istartswith=party_name_to_use)
    if len(matching_parties) == 0:
        party_name_for_creation = party_name_to_use.title()
        new_party = Organisation(name=party_name_for_creation,
                                 slug=slugify(party_name_for_creation),
                                 started=ApproximateDate(datetime.date.today().year),
                                 ended=None,
                                 kind=OrganisationKind.objects.get(slug='party'))
        maybe_save(new_party, **options)
        return new_party
    elif len(matching_parties) == 1:
        return matching_parties[0]
    else:
        raise Exception, "Multiple parties matched %s" % (party_name_to_use,)

def get_matching_place(place_name, place_kind, parliamentary_session):
    place_name_to_use = place_name_corrections.get(place_name, place_name)
    # We've normalized ward names to have a single space on either
    # side of a / or a -, so change API ward names to match:
    if place_kind.slug in ('ward', 'county'):
        place_name_to_use = re.sub(r'(\w) *([/-]) *(\w)', '\\1 \\2 \\3', place_name_to_use)
    # As with other place matching scripts here, look for the
    # slugified version to avoid problems with different separators:
    place_slug = slugify(place_name_to_use)
    if place_kind.slug == 'ward':
        place_slug = 'ward-' + place_slug
    elif place_kind.slug == 'county':
        place_slug += '-county'
    elif place_kind.slug == 'constituency':
        place_slug += '-2013'
    matching_places = Place.objects.filter(slug=place_slug,
                                           kind=place_kind,
                                           parliamentary_session=parliamentary_session)
    if not matching_places:
        raise Exception, "Found no place that matched: '%s' <%s> <%s>" % (place_slug, place_kind, parliamentary_session)
    elif len(matching_places) > 1:
        raise Exception, "Multiple places found that matched: '%s' <%s> <%s> - they were: %s" % (place_slug, place_kind, parliamentary_session, ",".join(str(p for p in matching_places)))
    else:
        return matching_places[0]

def parse_race_name(known_race_types, race_name):
    types_alternation = "|".join(re.escape(krt) for krt in known_race_types)
    m = re.search('^((%s) - )(.*?)\s+\(\d+\)$' % (types_alternation,), race_name)
    if not m:
        raise Exception, "Couldn't parse race:" + race_name
    return (m.group(2), m.group(3))

def make_new_person(candidate, **options):
    legal_name = (candidate.get('other_name', None) or '').title()
    if legal_name:
        legal_name += ' '
    legal_name += candidate['surname'].title()
    new_person = Person(legal_name=legal_name, slug=slugify(legal_name))
    maybe_save(new_person, **options)
    return new_person

def update_parties(person, api_party, **options):
    current_party_positions = person.position_set.all().currently_active().filter(title__slug='member').filter(organisation__kind__slug='party')
    if 'name' in api_party:
        # Then we should be checking that a valid party membership
        # exists, or create a new one otherwise:
        api_party_name = api_party['name']
        mz_party = get_matching_party(api_party_name, **options)
        need_to_create_party_position = True
        for party_position in (p for p in current_party_positions if p.organisation == mz_party):
            # If there's a current position in this party, that's fine
            # - just make sure that the end_date is 'future':
            party_position.end_date = ApproximateDate(future=True)
            maybe_save(party_position, **options)
            need_to_create_party_position = False
        for party_position in (p for p in current_party_positions if p.organisation != mz_party):
            # These shouldn't be current any more - end them when we
            # got the new data:
            party_position.end_date = new_data_approximate_date
            maybe_save(party_position, **options)
        if need_to_create_party_position:
            new_position = Position(title=PositionTitle.objects.get(name='Member'),
                                    organisation=mz_party,
                                    category='political',
                                    person=person,
                                    start_date=new_data_approximate_date,
                                    end_date=ApproximateDate(future=True))
            maybe_save(new_position, **options)
    else:
        # If there's no party specified, end all current party positions:
        for party_position in current_party_positions:
            party_position.end_date = new_data_approximate_date
            maybe_save(party_position, **options)

def maybe_save(o, **options):
    if options['commit']:
        o.save()
        print >> sys.stderr, 'Saving %s' % (o,)
    else:
        print >> sys.stderr, 'Not saving %s because --commit was not specified' % (o,)

class Command(NoArgsCommand):
    help = 'Update the database with aspirants from the IEBC website'

    option_list = NoArgsCommand.option_list + (
        make_option('--commit', action='store_true', dest='commit', help='Actually update the database'),
        )

    def handle_noargs(self, **options):

        api_key = hmac.new(IEBC_API_SECRET,
                           "appid=%s" % (IEBC_API_ID,),
                           hashlib.sha256).hexdigest()

        token_data = get_data(make_api_token_url(IEBC_API_ID, api_key))
        token = token_data['token']

        def url(path, query_filter=None):
            """A closure to avoid repeating parameters"""
            return make_api_url(path, IEBC_API_SECRET, token, query_filter)

        # Set up a mapping between the race names and the
        # corresponding PlaceKind and Position title:

        known_race_type_mapping = {
            "Governor": (PlaceKind.objects.get(slug='county'),
                         ParliamentarySession.objects.get(slug='s2013'),
                         PositionTitle.objects.get(slug__startswith='aspirant-governor')),
            "Senator": (PlaceKind.objects.get(slug='county'),
                        ParliamentarySession.objects.get(slug='s2013'),
                        PositionTitle.objects.get(slug__startswith='aspirant-senator')),
            "Women Representative": (PlaceKind.objects.get(slug='county'),
                                     ParliamentarySession.objects.get(slug='s2013'),
                                     PositionTitle.objects.get(slug__startswith='aspirant-women-representative')),
            "National Assembly Rep.": (PlaceKind.objects.get(slug='constituency'),
                                       ParliamentarySession.objects.get(slug='na2013'),
                                       PositionTitle.objects.get(slug__startswith='aspirant-mp')),
            "County Assembly Rep.": (PlaceKind.objects.get(slug='ward'),
                                     ParliamentarySession.objects.get(slug='na2013'),
                                     PositionTitle.objects.get(slug__startswith='aspirant-ward-representative')),
            }

        known_race_types = known_race_type_mapping.keys()

        # To get all the candidates, we iterate over each county,
        # constituency and ward, and request the candidates for each.

        cache_directory = os.path.join(sys.path[0], 'cache')

        mkdir_p(cache_directory)

        # ------------------------------------------------------------------------

        parties_cache_filename = os.path.join(cache_directory, 'parties')
        party_data = get_data_with_cache(parties_cache_filename, url('/party/'))

        party_names_api = set(d['name'].strip().encode('utf-8') for d in party_data['parties'])
        party_names_db = set(o.name.strip().encode('utf-8') for o in Organisation.objects.filter(kind__slug='party'))

        with open(os.path.join(data_directory, 'party-names.csv'), 'w') as fp:
            writer = csv.writer(fp)
            for t in itertools.izip_longest(party_names_api, party_names_db):
                writer.writerow(t)

        # ------------------------------------------------------------------------

        ward_data = get_data(url('/ward/'))

        wards_from_api = sorted(ward['name'].encode('utf-8') for ward in ward_data['region']['locations'])
        wards_from_db = sorted(p.name.encode('utf-8') for p in Place.objects.filter(kind__slug='ward'))

        with open(os.path.join(data_directory, 'wards-names.csv'), 'w') as fp:
            writer = csv.writer(fp)
            for t in itertools.izip_longest(wards_from_api, wards_from_db):
                writer.writerow(t)

        # ------------------------------------------------------------------------

        headings = ['API Name',
                    'API Party',
                    'API Place',
                    'API Candidate Code',
                    'Mz Legal Name',
                    'Mz Other Names',
                    'Mz URL',
                    'Mz Parties Ever',
                    'Mz Aspirant Ever',
                    'Mz Politician Ever',
                    'Mz ID']

        with open(os.path.join(sys.path[0], 'names-to-check.csv'), 'w') as fp:

            writer = csv.DictWriter(fp, headings)

            writer.writerow(dict((h, h) for h in headings))

            for area_type in 'county', 'constituency', 'ward':
                cache_filename = os.path.join(cache_directory, area_type)
                area_type_data = get_data_with_cache(cache_filename, url('/%s/' % (area_type)))
                areas = area_type_data['region']['locations']
                for i, area in enumerate(areas):
                    # Get the candidates for that area:
                    code = area['code']
                    candidates_cache_filename = os.path.join(cache_directory, 'candidates-for-' + area_type + '-' + code)
                    candidate_data = get_data_with_cache(candidates_cache_filename, url('/candidate/', query_filter='%s=%s' % (area_type, code)))
                    # print "got candidate_data:", candidate_data
                    for race in candidate_data['candidates']:
                        full_race_name = race['race']
                        race_type, place_name = parse_race_name(known_race_types, full_race_name)
                        place_kind, session, title = known_race_type_mapping[race_type]
                        place = get_matching_place(place_name, place_kind, session)
                        candidates = race['candidates']
                        for candidate in candidates:
                            first_names = candidate['other_name'] or ''
                            surname = candidate['surname'] or ''
                            person = get_person_from_names(first_names, surname)
                            if person:

                                print "got match to:", person
                                row = {}
                                row['API Name'] = first_names + ' ' + surname
                                party_data = candidate['party']
                                row['API Party'] = party_data['name'] if 'name' in party_data else ''
                                row['API Place'] = '%s (%s)' % (place_name, area_type)
                                row['API Candidate Code'] = candidate['code']
                                row['Mz Legal Name'] = person.legal_name.encode('utf-8')
                                row['Mz Other Names'] = person.other_names.encode('utf-8')
                                row['Mz URL'] = 'http://info.mzalendo.com' + person.get_absolute_url()
                                row['Mz Parties Ever'] = ', '.join(o.name for o in person.parties_ever())
                                for heading, positions in (('Mz Aspirant Ever', person.aspirant_positions_ever()),
                                                           ('Mz Politician Ever', person.politician_positions_ever())):
                                    row[heading] = ', '.join('%s at %s' % (p.title.name, p.place) for p in positions)
                                row['Mz ID'] = person.id
                                for heading in headings:
                                    print str(row[heading]) + ",",
                                print
                                writer.writerow(row)
