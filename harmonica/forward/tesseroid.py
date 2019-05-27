"""
Forward modelling for tesseroids
"""
import numpy as np
from numba import jit
from numpy.polynomial.legendre import leggauss

from ..constants import GRAVITATIONAL_CONST
from .point_mass import jit_point_masses_gravity, kernel_potential, kernel_g_radial

STACK_SIZE = 100
MAX_DISCRETIZATIONS = 100000
GLQ_DEGREES = [2, 2, 2]
DISTANCE_SIZE_RATII = {"potential": 1, "g_radial": 2.5}
KERNELS = {"potential": kernel_potential, "g_radial": kernel_g_radial}


def tesseroid_gravity(
    coordinates,
    tesseroid,
    density,
    field,
    distance_size_ratii=DISTANCE_SIZE_RATII,
    glq_degrees=GLQ_DEGREES,
    stack_size=STACK_SIZE,
    max_discretizations=MAX_DISCRETIZATIONS,
    three_dimensional_adaptive_discretization=False,
):
    """
    Compute gravitational field of a tesseroid on a single computation point

    Parameters
    ----------
    coordinates : list or 1d-array
        List or array containing ``longitude``, ``latitude`` and ``radius`` of a single
        computation point defined on a spherical geocentric coordinate system.
        Both ``longitude`` and ``latitude`` should be in degrees and ``radius`` in meters.
    tesseroid : list or 1d-array
        Geocentric spherical coordinates of the tesseroid: ``w``, ``e``, ``s``, ``n``,
        ``bottom``, ``top``.
        The longitudinal and latitudinal boundaries should be in degrees, while the
        radial ones must be in meters.
    density : float
        Density of the single tesseroid in kg/m^3.
    field: str
        Gravitational field that wants to be computed.
        The available fields are:

        - Gravitational potential: ``potential``
        - Radial acceleration: ``g_radial``

    distance_size_ratio : dict (optional)
        Dictionary containing distance-size ratii for each gravity field used on the
        adaptive discretization algorithm.
        Values must be the available fields and keys should be the desired distance-size
        ratio.
        The greater the distance-size ratio, more discretizations will occur, increasing
        the accuracy of the numerical approximation but also the computation time.
    glq_degrees : list (optional)
        List containing the GLQ degrees used on each direction:
        ``glq_degree_longitude``, ``glq_degree_latitude``, ``glq_degree_radius``.
        The GLQ degree specifies how many point masses will be created along each
        direction.
        Increasing the GLQ degree will increase the accuracy of the numerical
        approximation, but also the computation time.
        Default ``[2, 2, 2]``.
    stack_size : int (optional)
        Size of the tesseroid stack used on the adaptive discretization algorithm.
        If the algorithm will perform too many splits, please increase the stack size.
    max_discretizations : int (optional)
        Maximum number of splits made by the adaptive discretization algorithm.
        If the algorithm will perform too many splits, please increase the maximum
        number of splits.
    three_dimensional_adaptive_discretization : bool (optional)
        If ``False``, the adaptive discretization algorithm will split the tesseroid
        only on the horizontal direction. If ``True``, it will perform a three dimensional
        adaptive discretization, splitting the tesseroids on every direction.
        Default ``False``.

    Returns
    -------
    result : float
        Gravitational field generated by the tesseroid on the computation point.

    Examples
    --------

    >>> from harmonica import get_ellipsoid
    >>> # Get WGS84 ellipsoid
    >>> ellipsoid = get_ellipsoid()
    >>> # Define tesseroid of 1km of thickness with top surface on the mean Earth radius
    >>> thickness = 1000
    >>> top = ellipsoid.mean_radius
    >>> bottom = top - thickness
    >>> w, e, s, n = -1, 1, -1, 1
    >>> tesseroid = [w, e, s, n, bottom, top]
    >>> # Set a density of 2670 kg/m^3
    >>> density = 2670
    >>> # Define computation point located on the top surface of the tesseroid
    >>> coordinates = [0, 0, ellipsoid.mean_radius]
    >>> # Compute radial component of the gravitational gradient in mGal
    >>> tesseroid_gravity(coordinates, tesseroid, density, field="g_radial")
    -112.54539932616652

    """
    if field not in KERNELS:
        raise ValueError("Gravity field {} not recognized".format(field))
    # Get value of D (distance_size_ratio)
    distance_size_ratio = distance_size_ratii[field]
    # Convert coordinates and tesseroid to array to make Numba run only on Numpy arrays
    tesseroid = np.array(tesseroid)
    coordinates = np.array(coordinates)
    # Sanity checks for tesseroid and computation point
    _check_tesseroid(tesseroid)
    _check_point_outside_tesseroid(coordinates, tesseroid)
    # Initialize arrays to perform memory allocation only once
    stack = np.empty((stack_size, 6))
    small_tesseroids = np.empty((max_discretizations, 6))
    # Apply adaptive discretization on tesseroid
    n_splits, error = _adaptive_discretization(
        coordinates,
        tesseroid,
        distance_size_ratio,
        stack,
        small_tesseroids,
        radial_discretization=three_dimensional_adaptive_discretization,
    )
    if error == -1:
        raise OverflowError("Stack Overflow. Try to increase the stack size.")
    elif error == -2:
        raise OverflowError(
            "Small Tesseroids Overflow. Try to increase the maximum number of splits."
        )
    # Get GLQ unscaled nodes, weights and number of nodes for each small tesseroid
    n_nodes, glq_nodes, glq_weights = glq_nodes_weights(glq_degrees)
    # Get total number of point masses and initialize arrays
    n_point_masses = n_nodes * n_splits
    point_masses = np.empty((3, n_point_masses))
    weights = np.empty(n_point_masses)
    # Get equivalent point masses
    tesseroids_to_point_masses(
        small_tesseroids[:n_splits], glq_nodes, glq_weights, point_masses, weights
    )
    # Compute gravity fields
    longitude_p, latitude_p, radius_p = (i.ravel() for i in point_masses[:3])
    masses = density * weights
    result = jit_point_masses_gravity(
        coordinates, longitude_p, latitude_p, radius_p, masses, KERNELS[field]
    )
    result *= GRAVITATIONAL_CONST
    # Convert to more convenient units
    if field == "g_radial":
        result *= 1e5  # SI to mGal
    return result


@jit(nopython=True)
def tesseroids_to_point_masses(
    tesseroids, glq_nodes, glq_weights, point_masses, weights
):
    """
    Convert tesseroids to equivalent point masses on nodes of GLQ

    Each tesseroid is converted into a set of point masses located on the scaled nodes
    of the Gauss-Legendre Quadrature. The number of point masses created from each
    tesseroid is equal to the product of the GLQ degrees for each direction
    (:math:`N_r`, :math:`N_\lambda`, :math:`N_\phi`).
    It also compute a weight value for each point mass defined as the product of the GLQ
    weights for each direction (:math:`W_i^r`, :math:`W_j^\phi`, :math:`W_k^\lambda`),
    the scale constant :math:`A` and the :math:`\kappa` factor evaluated on the
    coordinates of the point mass.

    Parameters
    ----------
    tesseroids : 2d-array
        Array containing tesseroids boundaries.
    glq_nodes : list
        Unscaled location of GLQ nodes for each direction.
    glq_weights : list
        GLQ weigths for each node for each direction.
    point_masses : 2d-array
        Empty array with shape ``(3, n)``, where ``n`` is the total number of point
        masses computed as the product of number of tesseroids and the GLQ degrees for
        each direction.
        The location of the point masses will be located inside this array.
    weights : 1d-array
        Empty array with ``n`` elements.
        It will contain the weight constant for each point mass.

    """
    # Unpack nodes and weights
    lon_nodes, lat_nodes, rad_nodes = glq_nodes[:]
    lon_weights, lat_weights, rad_weights = glq_weights[:]
    # Recover GLQ degrees from nodes
    lon_glq_degree = len(lon_nodes)
    lat_glq_degree = len(lat_nodes)
    rad_glq_degree = len(rad_nodes)
    # Convert each tesseroid to a point mass
    mass_index = 0
    for i in range(len(tesseroids)):
        w = tesseroids[i, 0]
        e = tesseroids[i, 1]
        s = tesseroids[i, 2]
        n = tesseroids[i, 3]
        bottom = tesseroids[i, 4]
        top = tesseroids[i, 5]
        A_factor = 1 / 8 * np.radians(e - w) * np.radians(n - s) * (top - bottom)
        for i in range(lon_glq_degree):
            for j in range(lat_glq_degree):
                for k in range(rad_glq_degree):
                    # Compute coordinates of each point mass
                    longitude = 0.5 * (e - w) * lon_nodes[i] + 0.5 * (e + w)
                    latitude = 0.5 * (n - s) * lat_nodes[j] + 0.5 * (n + s)
                    radius = 0.5 * (top - bottom) * rad_nodes[k] + 0.5 * (top + bottom)
                    kappa = radius ** 2 * np.cos(np.radians(latitude))
                    point_masses[0, mass_index] = longitude
                    point_masses[1, mass_index] = latitude
                    point_masses[2, mass_index] = radius
                    weights[mass_index] = (
                        A_factor
                        * kappa
                        * lon_weights[i]
                        * lat_weights[j]
                        * rad_weights[k]
                    )
                    mass_index += 1


def glq_nodes_weights(glq_degrees):
    """
    Calculate GLQ unscaled nodes, weights and total number of nodes

    Parameters
    ----------
    glq_degrees : list
        List of GLQ degrees for each direction: ``longitude``, ``latitude``, ``radius``.

    Returns
    -------
    n_nodes : int
        Total number of nodes computed as the product of the GLQ degrees.
    glq_nodes : list
        Unscaled GLQ nodes for each direction: ``longitude``, ``latitude``, ``radius``.
    glq_weights : list
        GLQ weights for each node on each direction: ``longitude``, ``latitude``,
        ``radius``.
    """
    # Unpack GLQ degrees
    lon_degree, lat_degree, rad_degree = glq_degrees[:]
    # Get number of point masses
    n_nodes = np.prod(glq_degrees)
    # Get nodes coordinates and weights
    lon_node, lon_weights = leggauss(lon_degree)
    lat_node, lat_weights = leggauss(lat_degree)
    rad_node, rad_weights = leggauss(rad_degree)
    # Reorder nodes and weights
    glq_nodes = [lon_node, lat_node, rad_node]
    glq_weights = [lon_weights, lat_weights, rad_weights]
    return n_nodes, glq_nodes, glq_weights


@jit(nopython=True)
def _adaptive_discretization(
    coordinates,
    tesseroid,
    distance_size_ratio,
    stack,
    small_tesseroids,
    radial_discretization=False,
):
    """
    Perform the adaptive discretization algorithm on a tesseroid

    It apply the three or two dimensional adaptive discretization algorithm on
    a tesseroid after a single computation point.

    Parameters
    ----------
    coordinates : array
        Array containing ``longitude``, ``latitude`` and ``radius`` of a single
        computation point.
    tesseroid : array
        Array containing the boundaries of the tesseroid.
    distance_size_ratio : float
        Value for the distance-size ratio. A greater value will perform more
        discretizations.
    stack : 2d-array
        Array with shape ``(6, stack_size)`` that will temporarly hold the small
        tesseroids that are not yet processed.
        If too many discretizations will take place, increase the ``stack_size``.
    small_tesseroids : 2d-array
        Array with shape ``(6, max_discretizations)`` that will contain every small
        tesseroid produced by the adaptive discretization algorithm.
        If too many discretizations will take place, increase the
        ``max_discretizations``.
    radial_discretization : bool (optional)
        If ``True`` the three dimensional adaptive discretization will be applied.
        If ``False`` the two dimensional adaptive discretization will be applied, i.e.
        the tesseroid will only be split on the ``longitude`` and ``latitude``
        directions.
        Default ``False``.

    Returns
    -------
    n_splits : int
        Total number of small tesseroids generated by the algorithm.
    error : int
    """
    # Create stack of tesseroids
    stack[0] = tesseroid
    stack_top = 0
    error = 0
    n_splits = 0
    while stack_top >= 0:
        # Pop the first tesseroid from the stack
        tesseroid = stack[stack_top]
        stack_top -= 1
        # Get its dimensions
        l_lon, l_lat, l_rad = _tesseroid_dimensions(tesseroid)
        # Get distance between computation point and center of tesseroid
        distance = _distance_tesseroid_point(coordinates, tesseroid)
        # Check inequality
        n_lon, n_lat, n_rad = 1, 1, 1
        if distance / l_lon < distance_size_ratio:
            n_lon = 2
        if distance / l_lat < distance_size_ratio:
            n_lat = 2
        if distance / l_rad < distance_size_ratio and radial_discretization:
            n_rad = 2
        # Apply discretization
        if n_lon * n_lat * n_rad > 1:
            # Raise error if stack overflow
            # Number of tesseroids in stack = stack_top + 1
            if (stack_top + 1) + n_lon * n_lat * n_rad > stack.shape[0]:
                error = -1
                return n_splits, error
            stack_top = _split_tesseroid(
                tesseroid, n_lon, n_lat, n_rad, stack, stack_top
            )
        else:
            # Raise error if small_tesseroids overflow
            if n_splits + 1 > small_tesseroids.shape[0]:
                error = -2
                return n_splits, error
            small_tesseroids[n_splits] = tesseroid
            n_splits += 1
    return n_splits, error


@jit(nopython=True)
def _split_tesseroid(tesseroid, n_lon, n_lat, n_rad, stack, stack_top):
    """
    Split tesseroid along each dimension
    """
    w, e, s, n, bottom, top = tesseroid[:]
    # Compute differential distance
    d_lon = (e - w) / n_lon
    d_lat = (n - s) / n_lat
    d_rad = (top - bottom) / n_rad
    for i in range(n_lon):
        for j in range(n_lat):
            for k in range(n_rad):
                stack_top += 1
                stack[stack_top, 0] = w + d_lon * i
                stack[stack_top, 1] = w + d_lon * (i + 1)
                stack[stack_top, 2] = s + d_lat * j
                stack[stack_top, 3] = s + d_lat * (j + 1)
                stack[stack_top, 4] = bottom + d_rad * k
                stack[stack_top, 5] = bottom + d_rad * (k + 1)
    return stack_top


@jit(nopython=True)
def _tesseroid_dimensions(tesseroid):
    """
    Calculate the dimensions of the tesseroid.
    """
    w, e, s, n, bottom, top = tesseroid[:]
    w, e, s, n = np.radians(w), np.radians(e), np.radians(s), np.radians(n)
    latitude_center = (n + s) / 2
    l_lat = top * np.arccos(np.sin(n) * np.sin(s) + np.cos(n) * np.cos(s))
    l_lon = top * np.arccos(
        np.sin(latitude_center) ** 2 + np.cos(latitude_center) ** 2 * np.cos(e - w)
    )
    l_rad = top - bottom
    return l_lon, l_lat, l_rad


@jit(nopython=True)
def _distance_tesseroid_point(coordinates, tesseroid):
    """
    Calculate the distance between a computation point and the center of a tesseroid.
    """
    # Get coordinates of computation point
    longitude, latitude, radius = coordinates[:]
    # Get center of the tesseroid
    w, e, s, n, bottom, top = tesseroid[:]
    longitude_p = (w + e) / 2
    latitude_p = (s + n) / 2
    radius_p = (bottom + top) / 2
    # Convert angles to radians
    longitude, latitude = np.radians(longitude), np.radians(latitude)
    longitude_p, latitude_p = np.radians(longitude_p), np.radians(latitude_p)
    # Compute distance
    cosphi_p = np.cos(latitude_p)
    sinphi_p = np.sin(latitude_p)
    cosphi = np.cos(latitude)
    sinphi = np.sin(latitude)
    coslambda = np.cos(longitude_p - longitude)
    cospsi = sinphi_p * sinphi + cosphi_p * cosphi * coslambda
    distance = np.sqrt((radius - radius_p) ** 2 + 2 * radius * radius_p * (1 - cospsi))
    return distance


@jit(nopython=True)
def _check_tesseroid(tesseroid):
    "Check if tesseroid boundaries are well defined"
    w, e, s, n, bottom, top = tesseroid[:]
    if w >= e:
        raise ValueError(
            "Invalid tesseroid. The west boundary must be lower than the east one."
        )
    if s >= n:
        raise ValueError(
            "Invalid tesseroid. The south boundary must be lower than the north one."
        )
    if bottom <= 0 or top <= 0:
        raise ValueError(
            "Invalid tesseroid. The bottom and top radii must be greater than zero."
        )
    if bottom >= top:
        raise ValueError(
            "Invalid tesseroid. "
            + "The bottom radius boundary must be lower than the top one."
        )


@jit(nopython=True)
def _check_point_outside_tesseroid(coordinates, tesseroid):
    "Check if computation point is not inside the tesseroid"
    longitude, latitude, radius = coordinates[:]
    w, e, s, n, bottom, top = tesseroid[:]
    inside_longitude = bool(longitude > w and longitude < e)
    inside_latitude = bool(latitude > s and latitude < n)
    inside_radius = bool(radius > bottom and radius < top)
    if inside_longitude and inside_latitude and inside_radius:
        raise ValueError(
            "Found computation point inside tesseroid. "
            + "Computation points must be outside of tesseroids."
        )
