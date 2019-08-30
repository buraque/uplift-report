import os
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy
import scipy.stats
import s3fs
import xxhash

from datetime import datetime

from lib.const import __version__, TEST, CONTROL, CSV_SOURCE_MARKS_AND_SPEND, CSV_SOURCE_ATTRIBUTIONS, USER_ID_LENGTH

def log(*args):
    """
    Print something in a cleanly formatted fashion, with a timestamp
    :param args: A list of arguments to print. Internally, they will be passed to built-in print function

    :return None
    """
    print('[%s]:' % datetime.now(), *args)


class Helpers(object):
    """
    A class, wrapping a set of helper functions, providing functionality to calculate uplift report, given the
    setup arguments

    :param customer: Name of the customer the report is created for
    :param audiences: A list of audiences for which the report is going to be calculated
    :param revenue_event: An event which is going to be taken as a revenue event, e.g. "purchase"
    :param dates: Date range, for which the report is to be generated (use pandas.date_range to generate a range)
    :param groups: An optional dictionary of named campaign groups, by which the report should be split. Example:
            {
                "All US campaigns": [1234, 3456, 5678],
                "All EU campaigns": [4312, 5123],
            }
            (Default: None)
    :param per_campaign_results: Split uplift results per campaign (Default: False)
    :param use_converters_for_significance: Base statistical calculations off of unique converters instead
        of conversions (Default: false)
    :param use_deduplication: Enable deduplication heuristic for AppsFlyer (Default: False)
    :param confidence_level: Confidence level, required to calculate confidence intervals (Default: 0.95)

    :type customer: str
    :type dates: pandas.DatetimeIndex
    :type audiences: list[str]
    :type revenue_event: str
    :type groups: dict[str, list[int]]|None
    :type per_campaign_results: bool
    :type use_converters_for_significance: bool
    :type use_deduplication: bool
    :type confidence_level: float
    """

    def __init__(self, customer, audiences, revenue_event, dates, groups=None, per_campaign_results=False,
                 use_converters_for_significance=False, use_deduplication=False, export_user_ids=False,
                 confidence_level=0.95, bootstrap_size=10000):

        self.customer = customer

        self.audiences = audiences

        self.dates = dates

        self.revenue_event = revenue_event

        self.per_campaign_results = per_campaign_results
        self.groups = groups
        if self.groups is None:
            self.groups = {}

        self.use_converters_for_significance = use_converters_for_significance

        self.use_deduplication = use_deduplication

        self._csv_helpers = _CSVHelpers(
            customer=self.customer,
            revenue_event=self.revenue_event,
            export_user_ids=export_user_ids,
        )

        self.confidence_level = confidence_level

        if not bootstrap_size % 10:
            # we have a round number, round number - 1 gives the best results
            self.bootstrap_size = bootstrap_size - 1
        else:
            # it ain't round, just use it "as is"
            self.bootstrap_size = bootstrap_size

    @staticmethod
    def version():
        return __version__

    def load_marks_and_spend_data(self):
        """
        Load marks and spend data for given attributes (customer, audiences, timeframe etc.) into Pandas dataframe

        :return: Resulting dataframe, containing marks and spend data
        :rtype: pandas.DataFrame
        """
        df = pd.concat(
            [self._csv_helpers.read_csv(
                audience=audience,
                source=CSV_SOURCE_MARKS_AND_SPEND,
                date=date,
            ) for audience in self.audiences for date in self.dates],
            ignore_index=True,
            verify_integrity=True,
        )
        return df

    def load_attribution_data(self, marks_and_spend_df):
        """
        Load attribution data for given attributes (customer, audiences, timeframe etc.) into Pandas dataframe, given a
        marks and spend dataframe

        :param marks_and_spend_df: Marks and spend dataframe to filter users by

        :type marks_and_spend_df: pandas.DataFrame

        :return: Resulting attributions dataframe
        :rtype: pandas.DataFrame
        """
        marked_user_ids = self._marked(marks_and_spend_df)['user_id']
        df = pd.concat(
            [self._filter_by_user_ids(
                df=self._csv_helpers.read_csv(
                    audience=audience,
                    source=CSV_SOURCE_ATTRIBUTIONS,
                    date=date,
                    chunk_filter_fn=self._extract_revenue_events,
                ),
                user_ids=marked_user_ids,
            ) for audience in self.audiences for date in self.dates],
            ignore_index=True,
            verify_integrity=True,
        )

        # AppsFlyer sends some events twice - we want to remove the duplicates before the analysis
        if self.use_deduplication:
            df = self._drop_duplicates_in_attributions(df=df, max_timedelta=pd.Timedelta('1 minute'))

        return df

    def uplift_report(self, marks_and_spend_df, attributions_df):
        """
        Calculate and display uplift report for the data set as a whole
        This takes the whole data set and calculates uplift KPIs.

        :param marks_and_spend_df: Marks and spend dataframe
        :param attributions_df: Attributions dataframe, filtered by marks and spend (see load_attribution_data method)

        :type marks_and_spend_df: pandas.DataFrame
        :type attributions_df: pandas.DataFrame

        :return: Report dataframe, containing uplift report information, according to the configuration
        :rtype: pandas.DataFrame
        """
        # calculate the total result:
        report_df = self._uplift(
            marks_and_spend_df=marks_and_spend_df,
            attributions_df=attributions_df,
            index_name="total",
        )

        # if there are groups filter the events against the per campaign groups and generate report
        if report_df is not None and self.groups:
            for name, campaigns in self.groups.items():
                group_df = marks_and_spend_df[marks_and_spend_df.campaign_id.isin(campaigns)]
                report_df[name] = self._uplift(
                    marks_and_spend_df=group_df,
                    attributions_df=attributions_df,
                    index_name=name,
                )

        if report_df is not None and self.per_campaign_results:
            campaigns = marks_and_spend_df['campaign_id'].unique()
            for campaign in campaigns:
                name = "c_{0}".format(campaign)
                campaign_df = marks_and_spend_df[marks_and_spend_df.campaign_id == campaign]
                report_df[name] = self._uplift(
                    marks_and_spend_df=campaign_df,
                    attributions_df=attributions_df,
                    index_name=name,
                )

        return report_df

    @staticmethod
    def export_csv(df, file_name):
        """
        Export a Pandas dataframe to file by given path and start this file's download, if applicable (run in Google
        Colab)

        :param df: Dataframe to export
        :param file_name: File name, where to export the dataframe to

        :type df: pandas.DataFrame
        :type file_name: str

        :return: None
        """
        df.to_csv(file_name)

        log('Stored results as a local CSV file', file_name)

        try:
            import google.colab

            log('The download of the results file should start automatically')
            google.colab.files.download(file_name)
        except ImportError:
            # We are not in the colab, no need to run the download
            pass

    @staticmethod
    def _extract_revenue_events(df, revenue_event):
        """
        Only keep rows where the event is a revenue event and drop the partner_event column afterwards
        """
        df = df[df.partner_event == revenue_event]
        return df.drop(columns=['partner_event'])

    @staticmethod
    def _filter_by_user_ids(df, user_ids):
        if 'user_id' in df.columns:
            return df[df['user_id'].isin(user_ids)]
        else:
            return df

    @staticmethod
    def _drop_duplicates_in_attributions(df, max_timedelta):
        """
        # Clean the data

        Due to some inconsistencies in the measurement we need to clean the data before analysis.

        ### Remove duplicated events coming from AppsFlyer

        AppsFlyer is sending us two revenue events if they attribute the event to us. One of the events they send us
        does not contain attribution information and the other one does. Sadly, it is not possible for us to distinguish
        correctly if an event is a duplicate or if the user actually triggered two events with nearly the same
        information.
        Therefore we rely on a heuristic. We consider an event a duplicate if the user and revenue are equal and the
        events are less than a minute apart.
        """
        sorted_values = df.sort_values(['user_id', 'revenue_eur'])

        # Get values of the previous row
        sorted_values['last_ts'] = sorted_values['ts'].shift(1)
        sorted_values['last_user_id'] = sorted_values['user_id'].shift(1)
        sorted_values['last_revenue'] = sorted_values['revenue_eur'].shift(1)

        # Remove rows if the previous row has the same revenue_eur and user id and the ts are less than max_timedelta
        # apart
        filtered = sorted_values[
            (sorted_values['user_id'] != sorted_values['last_user_id']) |
            (sorted_values['revenue_eur'] != sorted_values['last_revenue']) |
            ((pd.to_datetime(sorted_values['ts']) - pd.to_datetime(sorted_values['last_ts'])) > max_timedelta)]

        return filtered[['ts', 'user_id', 'revenue_eur']]

    def _bootstrap_mean_ci(self, sample, plot=False):
        """
        Takes a sample and find the two-sided confidence interval of the sample, by bootstrap resampling.

        :param sample: The sample we have.
        :param plot: Whether to plot a histogram of the sample means with the calculated confidence interval

        :type sample: pandas.Series
        :type plot: bool

        :return: A tuple of lower and upper bounds of the confidence interval
        :rtype: (float, float)

        """
        # initialize the bootstrap samples
        # b_means = np.zeros(self.bootstrap_size)
        #
        # for i in range(self.bootstrap_size):
        #     b_means[i] = np.random.choice(sample, len(sample), replace=True).mean()

        bootstrapped_means = [np.random.choice(sample, len(sample), replace=True).mean() for _ in
                              range(self.bootstrap_size)]

        # b_means.sort()

        bootstrapped_means.sort()

        # l_bound = b_means[int(((1 - confidence_level) / 2) * (bootstrap_size + 1))]
        lower_bound = bootstrapped_means[int(((1 - self.confidence_level) / 2) * (self.bootstrap_size + 1))]

        # u_bound = b_means[int(((1 - confidence_level) / 2 + confidence_level) * (bootstrap_size + 1))]
        upper_bound = bootstrapped_means[int(((1 - self.confidence_level) / 2 + self.confidence_level) *
                                             (self.bootstrap_size + 1))]
        if plot:
            self._plot_ci(
                bootstrapped_means=bootstrapped_means,
                sample_mean=sample.mean(),
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
        return lower_bound, upper_bound

    def _plot_ci(self, bootstrapped_means, sample_mean, lower_bound, upper_bound):
        plt.hist(bootstrapped_means, bins=200)
        plt.vlines(upper_bound, ymin=0, ymax=self.bootstrap_size / 50, color='red')
        plt.vlines(lower_bound, ymin=0, ymax=self.bootstrap_size / 50, color='red')
        plt.vlines(sample_mean, ymin=0, ymax=self.bootstrap_size / 50, color='yellow')  # for the sample mean
        plt.show()

    def _uplift(
            self, marks_and_spend_df, attributions_df, index_name, plot_bootstrap_distribution=False
    ):
        """
        Computes the uplift results. All incremental KPI estimates are accompanied by confidence intervals.
        Confidence intervals for revenue and conversions related numbers and KPIs will be estimated by bootstrapping, while
        for converters the binomial distribution will be used.

        :param marks_and_spend_df: Dataframe containing information on marks and spend
        :param attributions_df: Dataframe with each entry being a conversion event
        :param index_name: Name of the data in question. To label the output and warning messages.

        :type marks_and_spend_df: pandas.DataFrame
        :type attributions_df: pandas.DataFrame
        :type index_name: str

        :return: Dataframe containing numbers and KPIs from the data.
        :rtype: pandas.DataFrame
        """
        # filter for mark events
        marks_df = self._marked(marks_and_spend_df)

        # calculate the ad spend
        ad_spend = self._calculate_ad_spend(marks_and_spend_df)

        # join marks and revenue events
        merged_users_df = self._merge_into_users_df(marks_df=marks_df, attributions_df=attributions_df)
        grouped_users = merged_users_df.groupby(by='ab_test_group')

        test_users_df = grouped_users.get_group(TEST)
        test_group_size = len(test_users_df)

        if not test_group_size:
            log("WARNING: No users marked as test for ", index_name, 'skipping.. ')
            return None

        control_users_df = grouped_users.get_group(CONTROL)
        control_group_size = len(control_users_df)

        if not control_group_size:
            log("WARNING: No users marked as control for ", index_name, 'skipping.. ')
            return None

        test_revenue_micros = test_users_df['revenue_eur'].sum()
        test_conversions = test_users_df['conversion_count'].sum()
        test_converters = (test_users_df['conversion_count'] > 0).sum()

        control_revenue_micros = control_users_df['revenue_eur'].sum()
        control_conversions = control_users_df['conversion_count'].sum()
        control_converters = (control_users_df['conversion_count'] > 0).sum()

        # samples for bootstrapping
        control_group_conversions_sample = control_users_df['conversion_count']
        control_group_revenue_sample = control_users_df['revenue_eur']

        ratio = float(test_group_size) / float(control_group_size)

        # Converter KPIs
        scaled_control_converters = float(control_converters) * ratio
        no_treat_converters_l_bound = scipy.stats.binom.ppf(
            (1 - self.confidence_level) / 2,
            control_group_size,
            control_converters / control_group_size,
            )
        no_treat_converters_u_bound = scipy.stats.binom.ppf(
            ((1 - self.confidence_level) / 2) + self.confidence_level,
            control_group_size,
            control_converters / control_group_size,
            )

        incremental_converters_estimate = test_converters - control_converters * ratio

        incremental_converters_l_bound = test_converters - no_treat_converters_u_bound * ratio
        incremental_converters_u_bound = test_converters - no_treat_converters_l_bound * ratio

        cost_per_incremental_converter_estimate = ad_spend / incremental_converters_estimate

        cost_per_incremental_converter_l_bound = ad_spend / incremental_converters_u_bound
        cost_per_incremental_converter_u_bound = ad_spend / incremental_converters_l_bound

        if cost_per_incremental_converter_estimate < 0:
            cost_per_incremental_converter_estimate = 'Negative incremental converters estimate'
        if cost_per_incremental_converter_u_bound < 0:
            cost_per_incremental_converter_l_bound = 'CI contains negative value, cannot be intepreted'
            cost_per_incremental_converter_u_bound = 'CI contains negative value, cannot be intepreted'

        # Conversion KPIs
        scaled_control_conversions = float(control_conversions) * ratio

        mean_no_treat_conversions_l_bound, mean_no_treat_conversions_u_bound = self._bootstrap_mean_ci(
            sample=control_group_conversions_sample,
            plot=plot_bootstrap_distribution,
        )

        scaled_no_treat_conversions_l_bound = mean_no_treat_conversions_l_bound * control_group_size * ratio
        scaled_no_treat_conversions_u_bound = mean_no_treat_conversions_u_bound * control_group_size * ratio

        incremental_conversions_estimate = test_conversions - scaled_control_conversions

        incremental_conversions_l_bound = test_conversions - scaled_no_treat_conversions_u_bound
        incremental_conversions_u_bound = test_conversions - scaled_no_treat_conversions_l_bound

        icpa_estimate = ad_spend / incremental_conversions_estimate

        icpa_l_bound = ad_spend / incremental_conversions_u_bound
        icpa_u_bound = ad_spend / incremental_conversions_l_bound

        if icpa_estimate < 0:
            icpa_estimate = 'Negative incremental conversions estimate'
        if icpa_u_bound < 0:
            icpa_l_bound = icpa_u_bound = 'CI contains negative value, cannot be intepreted'

        no_treat_cvr_l_bound = scaled_no_treat_conversions_l_bound / test_group_size
        no_treat_cvr_u_bound = scaled_no_treat_conversions_u_bound / test_group_size

        uplift_estimate = 0

        test_cvr = test_conversions / test_group_size
        control_cvr = control_conversions / control_group_size

        if control_cvr > 0:
            uplift_estimate = test_cvr / control_cvr - 1
            uplift_l_bound = test_cvr / no_treat_cvr_u_bound - 1
            uplift_u_bound = test_cvr / no_treat_cvr_l_bound - 1

        # Revenue KPIs
        test_revenue = test_revenue_micros / 10 ** 6
        control_revenue = control_revenue_micros / 10 ** 6

        scaled_control_revenue = float(control_revenue) * ratio

        mean_no_treat_revenue_micro_l_bound, mean_no_treat_revenue_micro_u_bound = self._bootstrap_mean_ci(
            sample=control_group_revenue_sample,
            plot=plot_bootstrap_distribution,
        )

        scaled_no_treat_revenue_l_bound = mean_no_treat_revenue_micro_l_bound * control_group_size * ratio / 10 ** 6
        scaled_no_treat_revenue_u_bound = mean_no_treat_revenue_micro_u_bound * control_group_size * ratio / 10 ** 6

        incremental_revenue_estimate = test_revenue - scaled_control_revenue
        incremental_revenue_l_bound = test_revenue - scaled_no_treat_revenue_u_bound
        incremental_revenue_u_bound = test_revenue - scaled_no_treat_revenue_l_bound

        iroas_estimate = incremental_revenue_estimate / ad_spend
        iroas_l_bound = incremental_revenue_l_bound / ad_spend
        iroas_u_bound = incremental_revenue_u_bound / ad_spend

        rev_per_conversion_test = 0
        rev_per_conversion_control = 0

        if test_conversions > 0:
            rev_per_conversion_test = test_revenue / test_conversions
        if control_conversions > 0:
            rev_per_conversion_control = control_revenue / control_conversions

        # Output
        dataframe_dict = {
            "ad spend": ad_spend,
            "total revenue": test_revenue + control_revenue,
            "control group size": control_group_size,
            "test group size": test_group_size,
            "ratio test/control": ratio,
            "control converters": control_converters,
            "control converters (scaled)": scaled_control_converters,
            "test converters": test_converters,
            "incremental converters estimate": incremental_converters_estimate,
            f"incremental converters {CONFIDENCE_LEVEL * 100}% CI lower bound": incremental_converters_l_bound,
            f"incremental converters {CONFIDENCE_LEVEL * 100}% CI upper bound": incremental_converters_u_bound,
            "cost per incr. converter estimate": cost_per_incremental_converter_estimate,
            f"cost per incr. converter {CONFIDENCE_LEVEL * 100}% CI lower bound": cost_per_incremental_converter_l_bound,
            f"cost per incr. converter {CONFIDENCE_LEVEL * 100}% CI upper bound": cost_per_incremental_converter_u_bound,
            "control conversions": control_conversions,
            "control conversions (scaled)": scaled_control_conversions,
            "test conversions": test_conversions,
            "incremental conversions estimate": incremental_conversions_estimate,
            f"incremental converions {CONFIDENCE_LEVEL * 100}% CI lower bound": incremental_conversions_l_bound,
            f"incremental converions {CONFIDENCE_LEVEL * 100}% CI upper bound": incremental_conversions_u_bound,
            "iCPA estimate": icpa_estimate,
            f"iCPA {CONFIDENCE_LEVEL * 100}% CI lower bound": icpa_l_bound,
            f"iCPA {CONFIDENCE_LEVEL * 100}% CI upper bound": icpa_u_bound,
            "control CVR": control_cvr,
            "test CVR": test_cvr,
            "CVR uplift estimate": uplift_estimate,
            f"CVR uplift {CONFIDENCE_LEVEL * 100}% CI lower bound": uplift_l_bound,
            f"CVR uplift {CONFIDENCE_LEVEL * 100}% CI upper bound": uplift_u_bound,
            "control revenue": control_revenue,
            "control revenue (scaled)": scaled_control_revenue,
            "test revenue": test_revenue,
            "incremental revenue estimate": incremental_revenue_estimate,
            f"incremental revenue {CONFIDENCE_LEVEL * 100}% CI lower bound": incremental_revenue_l_bound,
            f"incremental revenue {CONFIDENCE_LEVEL * 100}% CI upper bound": incremental_revenue_u_bound,
            "iROAS estimate": iroas_estimate,
            f"iROAS {CONFIDENCE_LEVEL * 100}% CI lower bound": iroas_l_bound,
            f"iROAS {CONFIDENCE_LEVEL * 100}% CI upper bound": iroas_u_bound,
            "rev/conversions control": rev_per_conversion_control,
            "rev/conversions test": rev_per_conversion_test,
        }

        return pd.DataFrame(dataframe_dict, index=[index_name]).transpose()

    def _old_uplift(self, marks_and_spend_df, attributions_df, index_name, m_hypothesis=1):
        """
        # Uplift Calculation

        We calculate the incremental revenue and the iROAS in line with the
        [remerge whitepaper](https://drive.google.com/file/d/1PTJ93Cpjw1BeiVns8dTcs2zDDWmmjpdc/view). Afterwards we run
        a [chi squared test](https://en.wikipedia.org/wiki/Chi-squared_test) on the results to test for significance of
        the results, comparing conversion to per group uniques.
        """
        # filter for mark events
        marks_df = self._marked(marks_and_spend_df)

        # calculate group sizes
        test_group_size = marks_df[marks_df['ab_test_group'] == TEST]['user_id'].nunique()
        if test_group_size == 0:
            log("WARNING: No users marked as test for ", index_name, 'skipping.. ')
            return None

        control_group_size = marks_df[marks_df['ab_test_group'] == CONTROL]['user_id'].nunique()
        if control_group_size == 0:
            log("WARNING: No users marked as control for ", index_name, 'skipping.. ')
            return None

        # join marks and revenue events
        merged_users_df = self._merge_into_users_df(marks_df=marks_df, attributions_df=attributions_df)
        grouped_users = merged_users_df.groupby(by='ab_test_group')

        # init all KPIs with 0s first:
        test_revenue_micros = 0
        test_conversions = 0
        test_converters = 0

        control_revenue_micros = 0
        control_conversions = 0
        control_converters = 0

        # we might not have any events for a certain group in the time-period,
        if TEST in grouped_users.groups:
            test_users_df = grouped_users.get_group(TEST)
            test_revenue_micros = test_users_df['revenue_eur'].sum()
            test_conversions = test_users_df['conversion_count'].sum()
            test_converters = (test_users_df['conversion_count'] > 0).sum()

        if CONTROL in grouped_users.groups:
            control_users_df = grouped_users.get_group(CONTROL)
            control_revenue_micros = control_users_df['revenue_eur'].sum()
            control_conversions = control_users_df['conversion_count'].sum()
            control_converters = (control_users_df['conversion_count'] > 0).sum()

        # calculate KPIs
        test_revenue = test_revenue_micros / 10 ** 6
        control_revenue = control_revenue_micros / 10 ** 6

        ratio = float(test_group_size) / float(control_group_size)
        scaled_control_conversions = float(control_conversions) * ratio
        scaled_control_revenue_micros = float(control_revenue_micros) * ratio
        incremental_conversions = test_conversions - scaled_control_conversions
        incremental_revenue_micros = test_revenue_micros - scaled_control_revenue_micros
        incremental_revenue = incremental_revenue_micros / 10 ** 6
        incremental_converters = test_converters - control_converters * ratio

        # calculate the ad spend
        ad_spend = self._calculate_ad_spend(marks_and_spend_df)

        iroas = incremental_revenue / ad_spend
        icpa = ad_spend / incremental_conversions
        cost_per_incremental_converter = ad_spend / incremental_converters

        rev_per_conversion_test = 0
        rev_per_conversion_control = 0
        if test_conversions > 0:
            rev_per_conversion_test = test_revenue / test_conversions
        if control_conversions > 0:
            rev_per_conversion_control = control_revenue / control_conversions

        test_cvr = test_conversions / test_group_size
        control_cvr = control_conversions / control_group_size

        uplift = 0
        if control_cvr > 0:
            uplift = test_cvr / control_cvr - 1

        # calculate statistical significance
        control_successes, test_successes = control_conversions, test_conversions
        if self.use_converters_for_significance or max(test_cvr, control_cvr) > 1.0:
            control_successes, test_successes = control_converters, test_converters
        chi_df = pd.DataFrame({
            "conversions": [control_successes, test_successes],
            "total": [control_group_size, test_group_size]
        }, index=['control', 'test'])
        # CHI square calculation will fail with insufficient data
        # Fallback to no significance
        try:
            chi, p, _, _ = scipy.stats.chi2_contingency(
                pd.concat([chi_df.total - chi_df.conversions, chi_df.conversions], axis=1), correction=False)
        except:
            chi, p = 0, 1.0

        # bonferroni correction with equal weights - if we have multiple hypothesis:
        # https://en.wikipedia.org/wiki/Bonferroni_correction
        significant = p < 0.05 / m_hypothesis

        dataframe_dict = {
            "ad spend": ad_spend,
            "total revenue": test_revenue + control_revenue,
            "test group size": test_group_size,
            "test conversions": test_conversions,
            "test converters": test_converters,
            "test revenue": test_revenue,
            "control group size": control_group_size,
            "control conversions": control_conversions,
            "control_converters": control_converters,
            "control revenue": control_revenue,
            "ratio test/control": ratio,
            "control conversions (scaled)": scaled_control_conversions,
            "control revenue (scaled)": scaled_control_revenue_micros / 10 ** 6,
            "incremental conversions": incremental_conversions,
            "incremental converters": incremental_converters,
            "incremental revenue": incremental_revenue,
            "rev/conversions test": rev_per_conversion_test,
            "rev/conversions control": rev_per_conversion_control,
            "test CVR": test_cvr,
            "control CVR": control_cvr,
            "CVR Uplift": uplift,
            "iROAS": iroas,
            "cost per incr. converter": cost_per_incremental_converter,
            "iCPA": icpa,
            "chi^2": chi,
            "p-value": p,
            "significant": significant
        }

        # show results as a dataframe
        return pd.DataFrame(
            dataframe_dict,
            index=[index_name],
        ).transpose()

    @staticmethod
    def _marked(df):
        """
        The dataframe created by `marked` will contain all mark events. Remerge marks users per campaign. If a user was
        marked once for an audience he will have the same group allocation for consecutive marks (different campaigns)
        unless manually reset on audience level.
        """
        if df.empty:
            return df

        mark_df = df[df.event_type == 'mark']

        # we dont need the event_type anymore (to save memory)
        mark_df = mark_df.drop(columns=['event_type'])

        sorted_mark_df = mark_df.sort_values('ts')

        deduplicated_mark_df = sorted_mark_df.drop_duplicates(['user_id'])

        return deduplicated_mark_df

    @staticmethod
    def _calculate_ad_spend(df):
        ad_spend_micros = df[(df.event_type == 'buying_conversion') & (df.ab_test_group == TEST)]['cost_eur'].sum()
        return ad_spend_micros / 10 ** 6

    @staticmethod
    def _merge_into_users_df(marks_df, attributions_df):
        """
        Takes the mark and revenue dataframes, merge them with left join into a dataframe with users as entries, where users
        without any attributions are included.
        Attribution events that come earlier than corresponding mark events will be removed;
        Users who have inconsistent group assignments between mark data and attribution data will be removed.

        :param marks_df: Dataframe with each entry being a mark event
        :param attributions_df: Dataframe with each entry being a conversion event

        :type marks_df: pandas.DataFrame
        :type attributions_df: pandas.DataFrame

        :return: Dataframe with each entry being a user
        :rtype: pandas.DataFrame
        """
        # All users from the mark side should be included.
        merged_df = pd.merge(marks_df, attributions_df, on='user_id', how='left')

        # There was no conversion, if there was no attribution timestamp
        merged_df['conversion_count'] = ~merged_df.ts_y.isnull() * 1

        # Remove the entries if the revenue event is earlier than the mark. Those shouldn't count.
        # But this can be wrong after we implement regular re-marks of users
        merged_df.conversion_count = (merged_df.ts_x < merged_df.ts_y) * merged_df.conversion_count
        merged_df.revenue_eur = (merged_df.ts_x < merged_df.ts_y) * merged_df.revenue_eur

        # note that we are using the group from marks here, because many users don't appear in attributions.
        # but it should not be a problem here because we are removing all mismatches anyway
        merged_df = merged_df[['user_id', 'ab_test_group', 'conversion_count', 'revenue_eur']]

        # create a dataframe with users as entries.
        users_df = merged_df.groupby('user_id').agg({
            'ab_test_group': 'first',
            'conversion_count': sum,
            'revenue_eur': sum,
        })

        return users_df


class _CSVHelpers(object):
    def __init__(self, customer, revenue_event, chunk_size=10 ** 6, export_user_ids=False):
        """
        Internal class, containing technical read-write related methods and helpers
        :param customer: Name of the customer the report is created for
        :param revenue_event: An event which is going to be taken as a revenue event, e.g. "purchase"
        :param chunk_size: How many lines should be taken for a single chunk during reads

        :type customer: str
        :type revenue_event: str
        :type chunk_size: int
        """
        self.customer = customer

        self.revenue_event = revenue_event

        self.chunk_size = chunk_size
        self.export_user_ids = export_user_ids

        # columns to load from CSV
        self.columns = dict()
        self.columns[CSV_SOURCE_MARKS_AND_SPEND] = ['ts', 'user_id', 'ab_test_group', 'campaign_id', 'cost_eur',
                                                    'event_type']
        self.columns[CSV_SOURCE_ATTRIBUTIONS] = ['ts', 'user_id', 'partner_event', 'revenue_eur']

    def _export_user_ids(self, date, audience, test_users, control_users):
        if self.export_user_ids:
            Helpers.export_csv(test_users, '{}_{}-{}.csv'.format(audience, date, 'test_users'))
            Helpers.export_csv(control_users, '{}_{}-{}.csv'.format(audience, date, 'control_users'))

    def read_csv(self, audience, source, date, chunk_filter_fn=None):
        """
        Helper to download CSV files, convert to DF and print time needed.
        Caches files locally and on S3 to be reused.
        """
        now = datetime.now()

        date_str = date.strftime('%Y%m%d')

        cache_folder = "cache-v{0}".format(__version__)
        if self.export_user_ids:
            cache_folder += "-user-export"

        filename = '{0}/{1}/{2}.csv.gz'.format(
            self._audience_data_path(audience),
            source,
            date_str,
        )

        # local cache
        cache_dir = '{0}/{1}/{2}'.format(
            cache_folder,
            audience,
            source,
        )

        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        cache_file_name = '{0}/{1}.parquet'.format(
            cache_dir,
            date_str,
        )

        # s3 cache (useful if we don't have enough space on the Colab instance)
        s3_cache_file_name = '{0}/{1}/{2}/{3}.parquet'.format(
            self._audience_data_path(audience),
            source,
            cache_folder,
            date_str,
        )

        if source == CSV_SOURCE_ATTRIBUTIONS:
            cache_file_name = '{0}/{1}-{2}.parquet'.format(
                cache_dir,
                date_str,
                self.revenue_event,
            )

            # s3 cache (useful if we don't have enough space on the Colab instance)
            s3_cache_file_name = '{0}/{1}/{2}/{3}-{4}.parquet'.format(
                self._audience_data_path(audience),
                source,
                cache_folder,
                date_str,
                self.revenue_event,
            )

        fs = s3fs.S3FileSystem(anon=False)
        fs.connect_timeout = 10  # defaults to 5
        fs.read_timeout = 30  # defaults to 15

        columns = self.columns.get(source)

        if os.path.exists(cache_file_name):
            log('loading from', cache_file_name)
            ret, test_users, control_users = self._from_parquet_corrected(
                file_name=cache_file_name,
                s3_file_name=s3_cache_file_name,
                fs=fs,
                columns=columns,
            )
            self._export_user_ids(date=date, audience=audience, test_users=test_users, control_users=control_users)
            return ret

        if fs.exists(path=s3_cache_file_name):
            log('loading from S3 cache', s3_cache_file_name)

            # Download the file to local cache first to avoid timeouts during the load.
            # This way, if they happen, restart will be using local copies first.
            fs.get(s3_cache_file_name, cache_file_name)

            log('stored S3 cache file to local drive, loading', cache_file_name)

            ret, test_users, control_users = self._from_parquet_corrected(
                file_name=cache_file_name,
                s3_file_name=s3_cache_file_name,
                fs=fs,
                columns=columns,
            )
            self._export_user_ids(date=date, audience=audience, test_users=test_users, control_users=control_users)
            return ret

        log('start loading CSV for', audience, source, date)
        log('filename', filename)

        read_csv_kwargs = {'chunksize': self.chunk_size}
        if columns:
            read_csv_kwargs['usecols'] = columns

        df = pd.DataFrame()
        test_users = pd.DataFrame()
        control_users = pd.DataFrame()

        if not fs.exists(path=filename):
            log('WARNING: no CSV file at for: ', audience, source, date, ', skipping the file: ', filename)
            return df

        for chunk in pd.read_csv(filename, escapechar='\\', low_memory=False, **read_csv_kwargs):
            if chunk_filter_fn:
                filtered_chunk = chunk_filter_fn(chunk, self.revenue_event)
            else:
                filtered_chunk = chunk

            if source != CSV_SOURCE_ATTRIBUTIONS:
                # we are not interested in events that do not have a group amongst non-attribution events
                filtered_chunk = filtered_chunk[filtered_chunk['ab_test_group'].isin(['test', 'control'])]

            # remove events without a user id
            filtered_chunk = filtered_chunk[filtered_chunk['user_id'].str.len() == USER_ID_LENGTH]

            if self.export_user_ids:
                test_users_chunk = filtered_chunk[filtered_chunk['ab_test_group'] == TEST][['user_id']].drop_duplicates()
                control_users_chunk = filtered_chunk[filtered_chunk['ab_test_group'] == CONTROL][['user_id']].drop_duplicates()
                test_users = pd.concat([test_users, test_users_chunk], ignore_index=True, verify_integrity=True)
                control_users = pd.concat([control_users, control_users_chunk], ignore_index=True, verify_integrity=True)

            filtered_chunk = self._improve_types(filtered_chunk)

            df = pd.concat([df, filtered_chunk],
                           ignore_index=True, verify_integrity=True)

        log('finished loading CSV for', date.strftime('%d.%m.%Y'), 'took', datetime.now() - now)

        self._export_user_ids(date=date, audience=audience, test_users=test_users, control_users=control_users)

        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)

        log('caching local as parquet', cache_file_name)
        self._to_parquet(df, cache_file_name)

        # write it to the S3 cache folder as well
        log('caching on S3 as parquet', s3_cache_file_name)
        self._to_parquet(df, s3_cache_file_name)

        return df

    def _audience_data_path(self, audience):
        return "s3://remerge-customers/{0}/uplift_data/{1}".format(
            self.customer,
            audience,
        )

    @staticmethod
    def _to_parquet(df, file_name):
        """
        parquet save and load helper
        """
        df.to_parquet(file_name, engine='pyarrow')

    @staticmethod
    def _improve_types(df):
        """
        Use more memory efficient types for ts,user_id and ab_test_group
        """
        df['ts'] = pd.to_datetime(df['ts'])
        df['ts'] = (df['ts'].astype('int64') / 1e9).astype('int32')
        df['user_id'] = df['user_id'].apply(xxhash.xxh64_intdigest).astype('int64')
        if 'ab_test_group' in df.columns:
            df['ab_test_group'] = df['ab_test_group'].transform(lambda g: g == 'test')
        return df

    @staticmethod
    def _from_parquet(filename):
        return pd.read_parquet(filename, engine='pyarrow')

    def _from_parquet_corrected(self, file_name, s3_file_name, fs, columns):
        """
        A little "hack" to convert old file on the fly
        """
        df = _CSVHelpers._from_parquet(file_name)
        update_cache = False
        if columns:
            to_drop = list(set(df.columns.values) - set(columns))
            if to_drop:
                df = df.drop(columns=to_drop)
                update_cache = True

        test_users = pd.DataFrame()
        control_users = pd.DataFrame()
        if self.export_user_ids:
            test_users = df[df['ab_test_group'] == TEST][['user_id']].drop_duplicates()
            control_users = df[df['ab_test_group'] == CONTROL][['user_id']].drop_duplicates()

        if df['ts'].dtype != 'int32':
            df = _CSVHelpers._improve_types(df)
            update_cache = True

        if update_cache:
            log('rewritting cached file with correct types (local and S3)', file_name, s3_file_name)
            _CSVHelpers._to_parquet(df=df, file_name=file_name)
            fs.put(file_name, s3_file_name)

        return df, test_users, control_users
