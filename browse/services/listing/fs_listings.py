"""arXiv listing backed by files.

Due to use of CloudPathLib these can be either local files or cloud object
stores.

"""

import logging
import re
from datetime import date, datetime
from typing import List, Literal, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from arxiv import taxonomy
from arxiv.base.globals import get_application_config
from browse.services import APath, fs_check
from browse.services.listing import (Listing, ListingCountResponse,
                                     ListingItem, ListingNew, ListingService,
                                     MonthCount, NotModifiedResponse,
                                     gen_expires)
from cloudpathlib.anypath import to_anypath
from werkzeug.exceptions import BadRequest

from .parse_listing_file import get_updates_from_list_file, ParsingMode
from .parse_new_listing_file import parse_new_listing_file
from .parse_listing_pastweek import parse_listing_pastweek

logger = logging.getLogger(__name__)
logger.level = logging.DEBUG

FS_TZ = ZoneInfo(get_application_config()["ARXIV_BUSINESS_TZ"])
"""Time used on the FS with the listing files."""

ListingFileType = Literal["new", "pastweek", "month"]
"""These are the listing file types."""


class FsListingFilesService(ListingService):
    """arXiv document listings via Filesystem."""

    def __init__(self, document_listing_path: str):
        self.listing_files_root = document_listing_path



    def _generate_listing_path(self, fileMode: ListingFileType, archiveOrCategory: str,
                               year: int, month: int) -> APath:
        """Create `Path` to a listing file.

        This just formats the string file name and returns a `Path`. It does
        not check if the file exists."""
        categorySuffix = ''
        archive = ''
        if archiveOrCategory in taxonomy.ARCHIVES:
            # Create listing file path with archive as <archive>/new
            archive = archiveOrCategory
        elif archiveOrCategory in taxonomy.CATEGORIES:
            # Get archive and create path - <archive>/new.<category>
            res = re.match('([^\\.]*)(?P<suffix>\\.[^\\.]*)$', archiveOrCategory)
            if res:
                suffix = res.group('suffix')
                categorySuffix = suffix
            archive = taxonomy.CATEGORIES[archiveOrCategory]['in_archive']
        else:
            raise BadRequest(f"Archive or category doesn't exist: {archiveOrCategory}")

        listingRoot = f'{self.listing_files_root}/{archive}/listings/'
        if fileMode == 'month':
            if len(str(year)) >= 4:
                if year < 2090:
                    yy = str(year)[2:]
                    listingFilePath = f'{listingRoot}{yy}{month:02d}'
                else:
                    listingFilePath = f'{listingRoot}{year}{month:02d}'
            elif len(str(year)) <= 2:
                listingFilePath = f'{listingRoot}{year:02d}{month:02d}'
            else:
                raise BadRequest("Bad year value {year}")
        else:
            listingFilePath = f'{listingRoot}{fileMode}{categorySuffix}'

        return to_anypath(listingFilePath)


    def _get_mtime(self, listingFilePath: APath) -> datetime:
        """Get the modify time fot specified file."""
        return datetime.fromtimestamp(listingFilePath.stat().st_mtime, tz=FS_TZ)



    def _current_y_m_em(self, year:int) -> Tuple[str,int,int]:
        """Gets `(currentYear, currentMonth, end_month)`"""
        # If current year, limit range to available months
        currentYear = str(datetime.now().year)[2:]
        currentMonth = datetime.now().month
        end_month = 12
        if currentYear == str(year):
            end_month = currentMonth
        return (currentYear, currentMonth, end_month)
    
    def _modified_since(self, if_modified_since: str, listingFile: APath) -> bool:
        """Returns whether data has been modified since `if_modified_since`."""
        if not listingFile.is_file():
            return False
        parsed = datetime.strptime(if_modified_since, '%a, %d %b %Y %H:%M:%S GMT')
        modTime = self._get_mtime(listingFile)
        return modTime > parsed


    def _list_articles_by_period(self,
                                 archiveOrCategory: str,
                                 yymmfiles: List[Tuple[int,int, APath]],
                                 skip: int,
                                 show: int,
                                 if_modified_since: Optional[str] = None,
                                 mode: ParsingMode = 'month')\
                                 -> Union[Listing, MonthCount, NotModifiedResponse]:
        """Gets listing for a list of `months`.

        This gets the listings for all the months in `months`. It works fine for
        getting just one month. Creating an archive listing for the year involves
        combining the listing files for all available months for the specified
        year.

        A category listing requires filtering these monthly listing files by the
        category.

        `if_modified_since` is the if_modified_since header value passed by the
        web client It should be in RFC 1123 format. This will return
        NotModifiedResponse if `if_modified_since` is not empty and any of the
        files related to `months` have been modified since then.

        Existing production year list links use two digit year.

        Parameters
        ----------
        archiveOrCategory : str
            A valid arxiv archive or category to get the listing for. Must not
            be empty.
        months : List[Tuple[int,int,APath]]
            The months to get the listings for. Tuple of (yy, mm, APath_to_listing_file)
            where both yy and mm are `int`. If yy or mm are 0 the
            result may lack pubdates.
        # tuple of (yy,mm) skip : int
        show : int
            The quantity of listings that need to be shown.
        if_modified_since : Optional[str]
            RFC 1123 format date of an if_modified_since header.
        mode: ParsingMode        
            Which type if listing is requested. One of ['new', 'month',
            'monthly_counts', 'year', 'pastweek']'month' works with a yymmfiles
            list greater than length 1. 'new' works only with a list of length 1.

        Returns
        -------
        Listing
            Combined listing response for all `months`

        Raises
        ------
        Exception
            If any listing file is missing. The only acceptable mising listing
            file is the one for the current year and month. That might not
            have been created yet if there has not yet been an announcement.

        """
        if mode == 'new' and len(yymmfiles) > 1:
            raise ValueError("When listing type  is 'new' yymmfiles must be size 1")

        currentYear, currentMonth, end_month = self._current_y_m_em(
            max([yy for yy,_,_ in yymmfiles]))
        
        if if_modified_since: # Check if-modified-since for months of interest
            if all([not self._modified_since(if_modified_since, lf)
                    for _,_, lf in yymmfiles]):
                return NotModifiedResponse(True, gen_expires())

        # Collect updates for each month
        all_listings: List[ListingItem] = []
        all_pubdates: List[Tuple[date,int]] = []
        for year, month, listingFile in yymmfiles:
            if not listingFile.is_file() and currentYear != str(year)\
               and currentMonth != str(month):
                # This is fine if new month and no announce has happened yet.
                raise Exception(f"Missing monthly listing file {listingFile}")

            response = get_updates_from_list_file(year, month, listingFile,
                                                  mode, archiveOrCategory)
            if not isinstance(response, Listing):
                return response
            
            all_listings.extend(response.listings)            
            if response.pubdates:
                all_pubdates.extend(response.pubdates)
            # else:
            #     pub_date = date(year, month, 1).strftime('%a, %d %b %Y')
            #     all_pubdates.append((pub_date, len(response.listings)))

        return Listing(listings=all_listings[skip:skip + show], # Adjust for skip/show
                       pubdates=all_pubdates,
                       count=len(all_listings),
                       expires= gen_expires())


    
    def list_articles_by_year(self,
                              archiveOrCategory: str,
                              year: int,
                              skip: int,
                              show: int,
                              if_modified_since: Optional[str] = None) -> Listing:
        """Get listing items for a whole year.

        if_modified_since is the if_modified_since header value passed by the web client
        It should be in RFC 1123 format.

        Creating a archive listing for the year involves combining
        the listing files for all available months for the specified
        year. A category listing requires filtering these monthly
        listing files by the category.

        Existing production year list links use two digit year.
        """
        _, _, end_month = self._current_y_m_em(year)
        months = [(year, month) for month in range(1, end_month + 1)]
        possible = (
            (year, month, self._generate_listing_path('month', archiveOrCategory,
                                                      year, month))
            for year, month in months)
        yymmfiles = [(year, month, apath) for (year, month, apath) in possible
                     if apath.is_file()]
        return self._list_articles_by_period(archiveOrCategory, yymmfiles, skip,
                                             show, if_modified_since) # type: ignore


    def list_articles_by_month(self,
                               archiveOrCategory: str,
                               year: int,
                               month: int,
                               skip: int,
                               show: int,
                               if_modified_since: Optional[str] = None) -> Listing:
        """Get listings for a month.

        if_modified_since is the if_modified_since header value passed by the web client
        It should be in RFC 1123 format.

        The monthly listing for an archive maps to a single file. The monthly
        listing for categories is more work since all updates are
        included in the same montly listing file.
        """
        yymmfiles= [(year,month, self._generate_listing_path('month', archiveOrCategory,
                                                             year, month))]
        return self._list_articles_by_period(archiveOrCategory, yymmfiles, skip,
                                             show, if_modified_since) # type: ignore


    def list_new_articles(self,
                          archiveOrCategory: str,
                          skip: int,
                          show: int,
                          if_modified_since: Optional[str] = None)\
                          -> Union[ListingNew, NotModifiedResponse]:
        """Gets listings for the most recent announcement/publish.

        if_modified_since is the if_modified_since header value passed by the web client
        It should be in RFC 1123 format.

        The 'new' listing maps to a single file. The filename depends on whether
        the archiveOrCategory value is an archive or category listing.
        """
        file= self._generate_listing_path('new', archiveOrCategory, 0, 0)
        if if_modified_since and self._modified_since(if_modified_since, file):
            return NotModifiedResponse(True, gen_expires())
        else:
            rv =  parse_new_listing_file(file)
            rv.listings = rv.listings[skip:skip + show] # Adjust for skip/show
            return rv

    def list_pastweek_articles(self,
                               archiveOrCategory: str,
                               skip: int,
                               show: int,
                               if_modified_since: Optional[str] = None)\
                               -> Union[Listing, NotModifiedResponse]:
        """Gets listings for the 5 most recent announcement/publish.

        if_modified_since is the if_modified_since header value passed by the web client
        It should be in RFC 1123 format.

        The 'pastweek' listing maps to a single file. The filename depends on whether
        the archiveOrCategory value is an archive or category listing.
        """
        file = self._generate_listing_path('pastweek', archiveOrCategory, 0, 0)
        if if_modified_since and self._modified_since(if_modified_since, file):
            return NotModifiedResponse(True, gen_expires())
        else:
            rv = parse_listing_pastweek(file)
            rv.listings = rv.listings[skip:skip + show] # Adjust for skip/show
            return rv

    
    def monthly_counts(self, archive: str, year: int) -> ListingCountResponse:
        """Gets monthly listing counts for the year."""
        monthly_counts: List[MonthCount] = []
        new_cnt, cross_cnt = 0, 0
        currentYear, currentMonth, end_month = self._current_y_m_em(year)

        files = []
        for month in range(1, end_month + 1):
            file = to_anypath(
                self._generate_listing_path('month', archive, year, month))
            files.append( (month, file, file.is_file()) )

        _check_contiguous(year, files)

        for month, file, exists in files:
            if not exists:
                continue
            response = get_updates_from_list_file(year, month, file, 'monthly_counts'
                                                  # archive TODO Does this need archive?
                                                  )
            if isinstance(response, MonthCount):
                monthly_counts.append(response)
                new_cnt += response.new
                cross_cnt += response.cross


        return ListingCountResponse(month_counts=monthly_counts,
                                    new_count=new_cnt,
                                    cross_count= cross_cnt)


    def service_status(self)->List[str]:
        probs = fs_check(to_anypath(self.listing_files_root))
        return ["FsListingFilesService: {prob}" for prob in probs]


def _check_contiguous(year: int, files: List[Tuple[int, APath, bool]]) -> None:
    """For a year, check that month listing files are a contiguous block.

    Raises an exception of not.

    For a list of month files that make up a year's worth, we want to check that
    no files seem to be missing. But there are severl cases where an nonexistant
    month file would be expected.

    Could have:
    1: year is current year and only have Jan through current month
    2: archive started on a month other than Jan so only have start month to Dec
    3: archive ended on a month other than Dec so only have Jan through end month
    4: archive existed under a year, didn't start on Jan and didn't end in Dec

    Months should be a contiguous block.
    """

    # Look for contiguous block by comparing the month to the next month to see
    # if it went from off to on or on to off.  We add a 0-th month that is off
    # and a 13th month that is off so we expect always only one off-to-on and
    # one on-to-off. Anything else is rejected.

    from_state = [(0,None,False)] + files
    to_state = files + [(13,None,False)]
    data = zip(from_state, to_state)
    transitions = []
    for (from_mm, _, from_exists),(to_mm,_,to_exists) in data:
        if not from_exists and to_exists:
            transitions.append("on")
        elif from_exists and not to_exists:
            transitions.append("off")
        else:
            transitions.append("no_change")

    if [dy for dy in transitions if dy != "no_change"] == ["on", "off"]:
        return  # all good

    # TODO Better exceptions
    if all(["no_change"==dy for dy in transitions]):
        raise Exception(f"No data for year {year}")
    else:
        msg=" ".join([f"{mm}:{int(exists)}" for mm,_,exists in files])
        raise Exception(f"Missing listing month files for year {year}: {msg}")